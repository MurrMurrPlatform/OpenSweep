"""Memory retrieval: relevance ranking, and getting memories in front of runs.

Two regressions are pinned here, both of which made "agents that learn your
codebase" mostly aspirational:

1. **Memories were invisible to most runs.** `build_briefing` only injected
   memories anchored to `target_doc_uids`, so every run that targets paths or
   nothing at all — repo audits, deep scans, implement, fix — got a prompt
   with zero memories in it. The Discovery loop wrote knowledge the Delivery
   loop never saw. `test_repo_scoped_run_with_no_target_docs_still_gets_memories`
   is the explicit regression test.

2. **Retrieval was `query in title + body`.** No ranking: a rephrased query
   returned nothing, and a common word returned everything in write order.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import domains.docs.services.briefing as briefing_module
import domains.memory.services.memory_service as memory_service
from domains.docs.services.briefing import build_briefing
from domains.memory.services.memory_service import search_memory
from domains.memory.services.relevance import (
    idf_weights,
    rank_memories,
    relevance_score,
    score_memories,
    tokenize,
)

NOW = datetime(2026, 7, 26, tzinfo=UTC)


def _mem(uid, title, body="", *, days_old=1, anchor="", repo="r1"):
    return SimpleNamespace(
        uid=uid,
        repository_uid=repo,
        anchor_uid=anchor,
        title=title,
        body=body,
        source_run_uid="",
        fingerprint="",
        created_at=NOW - timedelta(days=days_old),
        updated_at=NOW - timedelta(days=days_old),
    )


def _uids(memories):
    return [m.uid for m in memories]


# ── Tokenisation ─────────────────────────────────────────────────────────


def test_tokenize_splits_paths_snake_case_and_camel_case():
    assert tokenize("src/api/handlers.py") == ["src", "api", "handlers"]
    assert tokenize("run_policy retry-budget") == ["run", "policy", "retry", "budget"]
    # Agents write about `SandboxService` and search for "sandbox service".
    assert tokenize("SandboxService") == ["sandbox", "service"]
    # Acronym runs stay whole rather than exploding into single letters.
    assert tokenize("HTTPClient timeout") == ["http", "client", "timeout"]


def test_tokenize_drops_function_words_and_extension_noise():
    assert tokenize("the worker is in the queue") == ["worker", "queue"]
    # A path-shaped query must not make every .py memory "relevant".
    assert "py" not in tokenize("src/worker.py")


def test_tokenize_of_empty_input_is_empty():
    assert tokenize("") == []
    assert tokenize("   ///  ") == []


# ── IDF weighting ────────────────────────────────────────────────────────


def test_rare_words_outweigh_ubiquitous_ones():
    corpus = [
        ["celery", "worker", "retry"],
        ["celery", "worker", "queue"],
        ["celery", "worker", "beat"],
        ["celery", "worker", "prefetch"],
    ]
    weights = idf_weights(corpus)
    assert weights["retry"] > weights["celery"]
    # Smoothed: even a term in every document keeps a positive weight, or a
    # single-memory repository would score everything at zero.
    assert weights["celery"] > 0


def test_idf_of_an_empty_corpus_is_empty():
    assert idf_weights([]) == {}


# ── Scoring ──────────────────────────────────────────────────────────────


def test_title_hits_outrank_body_hits():
    in_title = relevance_score(query="redis fixture", title="Redis fixture flake", body="")
    in_body = relevance_score(query="redis fixture", title="Worker notes", body="redis fixture")
    assert in_title > in_body > 0


def test_a_reworded_query_still_matches():
    """The headline retrieval bug: substring matching missed word reordering."""
    score = relevance_score(
        query="redis fixture flake",
        title="Flaky Redis fixture in the worker tests",
        body="",
    )
    assert score > 0


def test_an_unrelated_memory_scores_zero():
    assert (
        relevance_score(query="redis fixture", title="Terraform state locking", body="")
        == 0.0
    )


def test_partial_coverage_scores_below_full_coverage():
    both = relevance_score(query="redis fixture", title="Redis fixture", body="")
    one = relevance_score(query="redis fixture", title="Redis connection pool", body="")
    assert both > one > 0


def test_an_empty_query_scores_nothing_topically():
    assert relevance_score(query="", title="Anything", body="Anything") == 0.0
    # A query of nothing but stopwords is the same case, not a match-all.
    assert relevance_score(query="the and of", title="the and of", body="") == 0.0


def test_a_long_memory_does_not_win_by_length_alone():
    """Scores are normalised by the query's IDF mass, not by hit count, so
    padding a memory with words cannot buy it rank."""
    focused = _mem("focused", "Redis fixture flake", "Short.")
    padded = _mem(
        "padded",
        "Deployment notes",
        "redis " + " ".join(f"unrelated{i}" for i in range(200)),
    )
    ranked = rank_memories(query="redis fixture flake", memories=[padded, focused], now=NOW)
    assert _uids(ranked)[0] == "focused"


# ── Ranking ──────────────────────────────────────────────────────────────


def test_ranking_puts_the_on_point_memory_first():
    memories = [
        _mem("noise", "Nightly build cache is warmed by the deploy job"),
        _mem("target", "Celery retry budget is per-task, not per-queue"),
        _mem("weak", "The deploy job runs on the celery box"),
    ]
    ranked = rank_memories(query="celery retry budget", memories=memories, now=NOW)
    assert _uids(ranked)[0] == "target"
    assert "noise" not in _uids(ranked)  # shares no content word with the query


def test_recency_only_breaks_ties():
    old_relevant = _mem("old", "Celery retry budget is per-task", days_old=400)
    new_weak = _mem("new", "Celery box was resized", days_old=0)
    ranked = rank_memories(query="celery retry budget", memories=[new_weak, old_relevant], now=NOW)
    assert _uids(ranked)[0] == "old"
    # …but between two equally relevant memories, the fresher one wins.
    older = _mem("older", "Celery retry budget is per-task", days_old=400)
    newer = _mem("newer", "Celery retry budget is per-task", days_old=1)
    ranked = rank_memories(query="celery retry budget", memories=[older, newer], now=NOW)
    assert _uids(ranked)[0] == "newer"


def test_stale_memories_lose_rank_but_are_not_banished():
    """Staleness used to be a hard tier — every fresh memory, however
    irrelevant, sorted above every stale one. It is a penalty now."""
    stale_hit = _mem("stale", "Celery retry budget is per-task")
    fresh_hit = _mem("fresh", "Celery retry budget is per-task")
    ranked = rank_memories(
        query="celery retry budget",
        memories=[stale_hit, fresh_hit],
        stale_uids={"stale"},
        now=NOW,
    )
    assert _uids(ranked) == ["fresh", "stale"]  # demoted, still returned


def test_stale_penalty_cannot_filter_out_a_real_match():
    only_hit = _mem("stale", "Celery retry budget is per-task")
    ranked = rank_memories(
        query="celery retry budget", memories=[only_hit], stale_uids={"stale"}, now=NOW
    )
    assert _uids(ranked) == ["stale"]


def test_an_empty_query_returns_everything_newest_first():
    memories = [
        _mem("b", "Second", days_old=2),
        _mem("a", "First", days_old=1),
        _mem("c", "Third", days_old=3),
    ]
    assert _uids(rank_memories(query="", memories=memories, now=NOW)) == ["a", "b", "c"]


def test_ranking_respects_the_limit():
    memories = [_mem(f"m{i}", f"Celery retry budget note {i}", days_old=i) for i in range(20)]
    assert len(rank_memories(query="celery retry", memories=memories, limit=5, now=NOW)) == 5
    # A nonsense limit must not return an empty prompt section.
    assert len(rank_memories(query="celery retry", memories=memories, limit=0, now=NOW)) == 1


def test_ranking_is_deterministic_for_identical_memories():
    """Two memories written in the same second used to come back in driver
    order, which made the briefing text (and its tests) non-reproducible."""
    a = _mem("a", "Celery retry budget", days_old=1)
    b = _mem("b", "Celery retry budget", days_old=1)
    first = _uids(rank_memories(query="celery retry", memories=[a, b], now=NOW))
    second = _uids(rank_memories(query="celery retry", memories=[b, a], now=NOW))
    assert first == second == ["a", "b"]


def test_naive_timestamps_do_not_crash_the_ranker():
    naive = _mem("naive", "Celery retry budget")
    # Naive on purpose: neomodel hands back naive datetimes on some drivers,
    # and comparing one to an aware `now` raises TypeError.
    naive.updated_at = datetime(2026, 7, 20)
    assert _uids(rank_memories(query="celery retry", memories=[naive], now=NOW)) == ["naive"]


def test_score_memories_exposes_the_scores_for_callers():
    scored = score_memories(
        query="celery retry budget",
        memories=[_mem("hit", "Celery retry budget"), _mem("miss", "Terraform state")],
        now=NOW,
    )
    by_uid = {m.uid: score for score, m in scored}
    assert by_uid["hit"] > by_uid["miss"]


# ── search_memory (service layer) ────────────────────────────────────────


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def filter(self, **kwargs):
        return [
            r
            for r in self._rows
            if all(getattr(r, k, None) == v for k, v in kwargs.items())
        ]


def _patch_memories(monkeypatch, rows, *, change_times=None):
    monkeypatch.setattr(memory_service, "Memory", SimpleNamespace(nodes=_Nodes(rows)))

    async def _change_times(_repository_uid):
        return change_times or {}

    monkeypatch.setattr(memory_service, "_anchor_change_times", _change_times)


async def test_search_ranks_instead_of_substring_filtering(monkeypatch):
    _patch_memories(
        monkeypatch,
        [
            _mem("flake", "Flaky Redis fixture in the worker tests"),
            _mem("other", "Terraform state lives in the ops bucket"),
        ],
    )
    # No substring of this query appears in the title; the old implementation
    # returned nothing at all here.
    out = await search_memory(repository_uid="r1", query="redis fixture flake")
    assert _uids(out) == ["flake"]


async def test_search_scopes_to_the_repository_and_the_anchor(monkeypatch):
    _patch_memories(
        monkeypatch,
        [
            _mem("mine", "Celery retry budget", anchor="d1"),
            _mem("other-anchor", "Celery retry budget", anchor="d2"),
            _mem("other-repo", "Celery retry budget", repo="r2"),
        ],
    )
    out = await search_memory(repository_uid="r1", anchor_uid="d1")
    assert _uids(out) == ["mine"]
    # Without an anchor filter, both of this repo's memories come back.
    out = await search_memory(repository_uid="r1")
    assert set(_uids(out)) == {"mine", "other-anchor"}


async def test_search_marks_and_demotes_stale_memories(monkeypatch):
    stale = _mem("stale", "Celery retry budget", anchor="d1", days_old=5)
    fresh = _mem("fresh", "Celery retry budget", days_old=5)
    _patch_memories(
        monkeypatch, [stale, fresh], change_times={"d1": NOW - timedelta(days=1)}
    )
    out = await search_memory(repository_uid="r1", query="celery retry budget")
    assert _uids(out) == ["fresh", "stale"]
    assert [m.possibly_stale for m in out] == [False, True]


async def test_search_returns_full_bodies_and_honours_the_limit(monkeypatch):
    _patch_memories(
        monkeypatch,
        [_mem(f"m{i}", f"Celery retry budget {i}", body=f"body {i}") for i in range(8)],
    )
    out = await search_memory(repository_uid="r1", query="celery", limit=3)
    assert len(out) == 3
    assert all(m.body for m in out)


# ── Briefing injection ───────────────────────────────────────────────────


def _doc(uid, slug, *, repo="r1", title="", summary="", pinned=False, watch_paths=()):
    return SimpleNamespace(
        uid=uid,
        repository_uid=repo,
        slug=slug,
        title=title or slug,
        summary=summary,
        body="",
        pinned=pinned,
        archived=False,
        watch_paths=list(watch_paths),
        code_changed_at=None,
        last_reviewed_at=None,
    )


class _DocNodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)


def _dto(uid, title, body="", *, anchor="", stale=False):
    return SimpleNamespace(
        uid=uid, title=title, body=body, anchor_uid=anchor, possibly_stale=stale
    )


def _patch_briefing(monkeypatch, docs, memories_by_anchor):
    """Stand in for search_memory, recording how the briefing queried it.

    `memories_by_anchor` maps anchor_uid ("" = the repo-wide pass) to the
    DTOs that call should return.
    """
    calls: list[dict] = []
    monkeypatch.setattr(briefing_module, "Doc", SimpleNamespace(nodes=_DocNodes(docs)))

    async def _search(**kwargs):
        calls.append(kwargs)
        return list(memories_by_anchor.get(kwargs.get("anchor_uid", ""), []))

    monkeypatch.setattr(briefing_module, "search_memory", _search)
    return calls


async def test_repo_scoped_run_with_no_target_docs_still_gets_memories(monkeypatch):
    """THE regression: an audit/deep-scan/implement/fix run targets paths or
    nothing, never a Doc — and used to be briefed with zero memories, so the
    Delivery loop ran without anything the Discovery loop had learned."""
    calls = _patch_briefing(
        monkeypatch,
        [_doc("d1", "backend/api", watch_paths=["src/api"])],
        {"": [_dto("m1", "Celery retry budget is per-task")]},
    )
    out = await build_briefing(repository_uid="r1", target_paths=["src/api"])
    assert "Celery retry budget is per-task" in out
    # The repo-wide pass ran, and it was given the run's scope to rank with.
    repo_wide = [c for c in calls if not c.get("anchor_uid")]
    assert len(repo_wide) == 1
    assert "src/api" in repo_wide[0]["query"]


async def test_run_with_no_target_at_all_still_gets_memories(monkeypatch):
    """A run with neither doc nor path target (whole-repo sweep) is the most
    memory-starved case of all under the old behaviour."""
    calls = _patch_briefing(
        monkeypatch, [], {"": [_dto("m1", "Prod migrations are run by hand")]}
    )
    out = await build_briefing(repository_uid="r1")
    assert "Prod migrations are run by hand" in out
    # No scope to rank by — the ranker reads "" as "newest first", not "none".
    assert calls[0]["query"] == ""


async def test_target_anchored_memories_come_first(monkeypatch):
    calls = _patch_briefing(
        monkeypatch,
        [_doc("d1", "backend/api")],
        {
            "d1": [_dto("anchored", "Anchored fact", anchor="d1")],
            "": [_dto("repo", "Repo-wide fact")],
        },
    )
    out = await build_briefing(repository_uid="r1", target_doc_uids=["d1"])
    assert out.index("Anchored fact") < out.index("Repo-wide fact")
    assert [c.get("anchor_uid", "") for c in calls] == ["d1", ""]


async def test_a_memory_returned_by_both_passes_is_rendered_once(monkeypatch):
    dto = _dto("dup", "Shared fact", anchor="d1")
    _patch_briefing(monkeypatch, [_doc("d1", "backend/api")], {"d1": [dto], "": [dto]})
    out = await build_briefing(repository_uid="r1", target_doc_uids=["d1"])
    assert out.count("Shared fact") == 1


async def test_related_doc_titles_feed_the_ranking_query(monkeypatch):
    calls = _patch_briefing(
        monkeypatch,
        [
            _doc(
                "d1",
                "backend/queue",
                title="Queue workers",
                summary="Celery topology",
                watch_paths=["src/workers"],
            )
        ],
        {"": []},
    )
    await build_briefing(repository_uid="r1", target_paths=["src/workers"])
    query = calls[0]["query"]
    assert "src/workers" in query and "Queue workers" in query and "Celery topology" in query


async def test_stale_memories_are_labelled_in_the_prompt(monkeypatch):
    _patch_briefing(monkeypatch, [], {"": [_dto("m1", "Old fact", "body", stale=True)]})
    out = await build_briefing(repository_uid="r1")
    assert "possibly stale" in out


async def test_memory_section_is_capped_so_it_cannot_eat_the_prompt(monkeypatch):
    """The briefing is prepended to EVERY first-turn prompt on the repo, so
    unbounded memory injection would tax every run forever."""
    fat = [_dto(f"m{i}", f"Fact {i}", "x" * 5000) for i in range(20)]
    _patch_briefing(monkeypatch, [], {"": fat})
    out = await build_briefing(repository_uid="r1")
    assert len(out) < 10_000
    assert "full text via search_memory" in out  # clipped, not dropped
    assert "Fact 0" in out  # the best-ranked memory always survives the cap


async def test_no_memories_means_no_memory_section(monkeypatch):
    _patch_briefing(monkeypatch, [_doc("d1", "backend/api")], {})
    out = await build_briefing(repository_uid="r1", target_paths=["src/api"])
    assert "previous runs learned" not in out
