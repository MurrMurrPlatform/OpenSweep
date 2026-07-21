"""Area freshness — the doc-freshness flow ported to the Area map.

mark_areas_stale is exercised against faked Area nodes (match / no-match /
stale_paths cap / dedupe), and the shared refresh_docs_for_change hook is
pinned to actually call it.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from domains.areas.services import area_freshness
from domains.areas.services.area_freshness import _MAX_STALE_PATHS, mark_areas_stale


class _FakeArea:
    def __init__(self, uid, repository_uid, scope_paths, stale_paths=None):
        self.uid = uid
        self.repository_uid = repository_uid
        self.scope_paths = scope_paths
        self.stale_paths = list(stale_paths or [])
        self.code_changed_at = None
        self.saved = False

    async def save(self):
        self.saved = True


def _install(monkeypatch, areas):
    class _Nodes:
        @staticmethod
        async def all():
            return areas

    monkeypatch.setattr(area_freshness, "Area", SimpleNamespace(nodes=_Nodes))


async def test_marks_only_covering_areas_in_repo(monkeypatch):
    hit = _FakeArea("a-delivery", "repo-1", ["back_end/domains/delivery"])
    miss = _FakeArea("a-docs", "repo-1", ["back_end/domains/docs"])
    other_repo = _FakeArea("a-other", "repo-2", ["back_end/domains/delivery"])
    no_scope = _FakeArea("a-none", "repo-1", [])
    _install(monkeypatch, [hit, miss, other_repo, no_scope])

    result = await mark_areas_stale(
        "repo-1", ["back_end/domains/delivery/services/fix_run_service.py"]
    )
    assert result.areas_marked == 1 and result.errors == []
    assert hit.saved and hit.code_changed_at is not None
    assert hit.stale_paths == ["back_end/domains/delivery/services/fix_run_service.py"]
    assert not miss.saved and not other_repo.saved and not no_scope.saved


async def test_scope_matching_respects_slash_boundary(monkeypatch):
    a = _FakeArea("a-1", "repo-1", ["back_end/domains"])
    _install(monkeypatch, [a])
    result = await mark_areas_stale("repo-1", ["back_end/domains2/models.py"])
    assert result.areas_marked == 0 and not a.saved


async def test_empty_changed_paths_short_circuits(monkeypatch):
    # Guard runs before any DB access — safe without a fake store.
    result = await mark_areas_stale("repo-1", ["  ", ""])
    assert result.areas_marked == 0 and result.errors == []


async def test_stale_paths_accumulate_dedupe_and_cap(monkeypatch):
    existing = [f"src/f{i}.py" for i in range(_MAX_STALE_PATHS - 1)]
    a = _FakeArea("a-1", "repo-1", ["src"], stale_paths=existing)
    _install(monkeypatch, [a])
    now = datetime.now(UTC)

    result = await mark_areas_stale(
        "repo-1", ["src/f0.py", "src/new1.py", "src/new2.py"], now=now
    )
    assert result.areas_marked == 1
    assert a.code_changed_at == now
    assert len(a.stale_paths) == _MAX_STALE_PATHS  # capped
    assert a.stale_paths.count("src/f0.py") == 1  # deduped, not re-appended
    assert "src/new1.py" in a.stale_paths


async def test_one_bad_area_never_blocks_the_rest(monkeypatch):
    class _BrokenArea(_FakeArea):
        async def save(self):
            raise RuntimeError("boom")

    broken = _BrokenArea("a-bad", "repo-1", ["src"])
    good = _FakeArea("a-good", "repo-1", ["src"])
    _install(monkeypatch, [broken, good])

    result = await mark_areas_stale("repo-1", ["src/app.py"])
    assert result.areas_marked == 1
    assert good.saved
    assert result.errors and "a-bad" in result.errors[0]


async def test_refresh_docs_for_change_marks_areas(monkeypatch):
    """The shared change hook is the ONLY caller — pin that it reaches
    mark_areas_stale (lazy import, best-effort) alongside the doc pass."""
    from domains.agents.services import event_triggers
    from domains.docs.services import doc_freshness

    calls: list[tuple[str, list[str]]] = []

    async def _fake_mark_areas_stale(repository_uid, changed_paths, now=None):
        calls.append((repository_uid, changed_paths))
        return SimpleNamespace(areas_marked=0, errors=[])

    async def _fake_mark_docs_stale(repository_uid, changed_paths, now=None):
        return SimpleNamespace(docs_marked=0, errors=[])

    async def _no_auto_runs(*, repository_uid, changed_paths):
        return []

    monkeypatch.setattr(area_freshness, "mark_areas_stale", _fake_mark_areas_stale)
    monkeypatch.setattr(doc_freshness, "mark_docs_stale", _fake_mark_docs_stale)
    monkeypatch.setattr(
        event_triggers, "auto_run_candidates_for_change", _no_auto_runs
    )

    await event_triggers.refresh_docs_for_change(
        repository_uid="repo-1", changed_paths=["back_end/app.py", " "]
    )
    assert calls == [("repo-1", ["back_end/app.py"])]
