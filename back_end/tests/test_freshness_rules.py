"""The review axis — one predicate, one review stamp, one tracked-ness rule.

These pin `domains.freshness`, which replaced four copies of the same rule
(docs.models, areas.models, an inline re-expression in audit_selection, and a
Cypher mirror in attention_service). The copies agreed; nothing made them agree.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from domains.areas.models import area_is_stale, area_is_tracked
from domains.docs.models import doc_is_stale, doc_is_tracked
from domains.freshness import (
    STALE_WHERE,
    is_stale,
    is_tracked,
    mark_reviewed,
    node_is_stale,
)

NOW = datetime.now(UTC)
EARLIER = NOW - timedelta(days=1)


def test_is_stale_truth_table():
    # Never touched by a push is NOT stale — a page nothing has changed needs
    # no review, and treating it as stale would make the badge meaningless.
    assert is_stale(None, None) is False
    assert is_stale(None, NOW) is False
    # Changed but never reviewed IS stale.
    assert is_stale(NOW, None) is True
    # Changed after the review.
    assert is_stale(NOW, EARLIER) is True
    # Reviewed after the change.
    assert is_stale(EARLIER, NOW) is False
    # Exactly equal is not stale — the review is not strictly older.
    assert is_stale(NOW, NOW) is False


def test_doc_and_area_predicates_agree_with_the_shared_rule():
    """The two domain wrappers must stay pure delegations."""
    for code_changed, reviewed in (
        (None, None), (None, NOW), (NOW, None), (NOW, EARLIER), (EARLIER, NOW)
    ):
        node = SimpleNamespace(
            code_changed_at=code_changed, last_reviewed_at=reviewed
        )
        expected = is_stale(code_changed, reviewed)
        assert doc_is_stale(node) is expected
        assert area_is_stale(node) is expected
        assert node_is_stale(node) is expected


def test_is_tracked_distinguishes_unwatched_from_current():
    # The whole point: an unwatched node is NOT stale, but that is because
    # nothing can mark it — not because it is up to date.
    assert is_tracked([]) is False
    assert is_tracked(None) is False
    assert is_tracked(["src/a"]) is True
    assert doc_is_tracked(SimpleNamespace(watch_paths=[])) is False
    assert doc_is_tracked(SimpleNamespace(watch_paths=["a"])) is True
    assert area_is_tracked(SimpleNamespace(scope_paths=[])) is False
    assert area_is_tracked(SimpleNamespace(scope_paths=["a"])) is True
    # An untracked node reads as not-stale, which is exactly why callers must
    # branch on tracked-ness before rendering it green.
    assert is_stale(None, None) is False


def test_mark_reviewed_advances_the_stamp_and_drops_the_path_list():
    node = SimpleNamespace(last_reviewed_at=EARLIER, stale_paths=["src/a", "src/b"])
    mark_reviewed(node, NOW)
    assert node.last_reviewed_at == NOW
    # stale_paths is "what changed since you last looked" — meaningless now.
    assert node.stale_paths == []


async def test_stale_paths_keep_the_newest_not_the_oldest():
    """The cap used to shed the change that just landed and keep ancient
    history: `dict.fromkeys(existing + hits)` preserves insertion order, so a
    node at the cap froze its list forever and the tooltip stopped naming the
    push that set the badge."""
    from domains.repositories.services.path_matching import (
        MAX_STALE_PATHS,
        mark_nodes_stale,
    )

    node = SimpleNamespace(
        uid="n1",
        watch_paths=["src"],
        stale_paths=[f"src/old{i}.py" for i in range(MAX_STALE_PATHS)],
        code_changed_at=None,
        save=_noop_save,
    )
    await mark_nodes_stale([node], ["src/just_changed.py"], watch_attr="watch_paths")

    assert node.stale_paths[0] == "src/just_changed.py"
    assert len(node.stale_paths) == MAX_STALE_PATHS
    assert "src/old0.py" in node.stale_paths  # history kept, just demoted


async def test_stale_paths_promote_a_retouched_path_without_duplicating():
    from domains.repositories.services.path_matching import mark_nodes_stale

    node = SimpleNamespace(
        uid="n1",
        watch_paths=["src"],
        stale_paths=["src/a.py", "src/b.py"],
        code_changed_at=None,
        save=_noop_save,
    )
    await mark_nodes_stale([node], ["src/b.py"], watch_attr="watch_paths")

    assert node.stale_paths == ["src/b.py", "src/a.py"]


async def _noop_save():
    return None


def test_cypher_half_matches_the_python_rule():
    """The aggregate counts cannot round-trip through Python, so they
    interpolate this. It must name the same two fields and the same
    null-handling, or the attention panel silently disagrees with the board."""
    rendered = STALE_WHERE.format(n="a")
    assert "a.code_changed_at IS NOT NULL" in rendered
    assert "a.last_reviewed_at IS NULL" in rendered
    assert "a.code_changed_at > a.last_reviewed_at" in rendered
    # Renders for any binding name.
    assert STALE_WHERE.format(n="d").startswith("d.code_changed_at")
