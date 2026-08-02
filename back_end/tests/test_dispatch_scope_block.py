"""The scope block a document run is handed must be actionable on its own.

It used to print bare doc UIDs ("the documentation pages with uids a1, d4"),
but `read_doc` is keyed by SLUG and there is no uid-keyed read tool — so the
agent had to reverse every uid through `list_docs` before it could open
anything. These pin the rendering: slugs, the stale/archived markers, and the
"resolve every page" instruction that makes a run able to clear staleness.
"""

from types import SimpleNamespace

import pytest

from domains.agents.services.dispatch import _resolve_scope_docs, _scope_summary


def test_scope_block_lists_slugs_not_uids():
    out = _scope_summary(
        {"doc_uids": ["a1", "d4"]},
        {
            "a1": ("backend/queue-workers", "Queue workers", True, False),
            "d4": ("conventions", "Conventions", False, False),
        },
    )
    # The slug is what read_doc takes — the uid must not be what the agent sees.
    assert "backend/queue-workers (Queue workers)" in out
    assert "conventions (Conventions)" in out
    assert "a1" not in out and "d4" not in out
    # Both tools that can clear a page's stale flag are named.
    assert "read_doc(slug=…)" in out
    assert "propose_doc_edit" in out and "confirm_doc_current(slug=…)" in out


def test_scope_block_marks_stale_and_archived_pages():
    out = _scope_summary(
        {"doc_uids": ["s", "f", "a"]},
        {
            "s": ("stale/page", "Stale", True, False),
            "f": ("fresh/page", "Fresh", False, False),
            "a": ("old/page", "Retired", True, True),
        },
    )
    lines = {line.split(" ")[1]: line for line in out.splitlines() if line.startswith("- ")}
    assert "STALE" in lines["stale/page"]
    assert lines["fresh/page"].strip() == "- fresh/page (Fresh)"
    # Archived wins over stale: re-reviewing a retired page is wasted budget.
    assert "ARCHIVED" in lines["old/page"] and "STALE" not in lines["old/page"]


def test_scope_block_names_unresolvable_uids_instead_of_dropping_them():
    # A page deleted between dispatch and compose must not silently vanish from
    # a scope the caller asked for.
    out = _scope_summary({"doc_uids": ["ghost"]}, {})
    assert "(unknown page uid=ghost)" in out


def test_scope_block_falls_back_to_paths_and_whole_repo():
    assert _scope_summary({}) == "Scope: the whole repository."
    paths_only = _scope_summary({"paths": ["back_end/domains/delivery"]})
    assert "back_end/domains/delivery" in paths_only
    assert "documentation pages" not in paths_only


async def test_resolve_scope_docs_short_circuits_without_doc_uids(monkeypatch):
    """No doc scope must not cost a DB round-trip — every non-doc run hits this."""
    import domains.docs.models as doc_models

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("queried the DB for an empty doc scope")

    monkeypatch.setattr(doc_models.Doc, "nodes", SimpleNamespace(filter=_boom))
    assert await _resolve_scope_docs("r1", []) == {}


async def test_resolve_scope_docs_queries_only_the_scoped_uids(monkeypatch):
    import domains.docs.models as doc_models

    seen: dict = {}

    async def _filter(**kwargs):
        seen.update(kwargs)
        return [
            SimpleNamespace(
                uid="a1",
                slug="backend/queue-workers",
                title="Queue workers",
                archived=False,
                code_changed_at=None,
                last_reviewed_at=None,
                stale_paths=[],
            )
        ]

    monkeypatch.setattr(doc_models.Doc, "nodes", SimpleNamespace(filter=_filter))
    out = await _resolve_scope_docs("r1", ["a1", "a1", "gone"])

    # Scoped by uid rather than scanning (and loading the body of) every page.
    assert seen["repository_uid"] == "r1"
    assert seen["uid__in"] == ["a1", "gone"]  # deduped, order preserved
    assert out == {"a1": ("backend/queue-workers", "Queue workers", False, False)}
