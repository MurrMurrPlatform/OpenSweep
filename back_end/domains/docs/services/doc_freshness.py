"""Doc freshness — webhook-driven (KNOWLEDGE_V3_DOCUMENTATION.md §9).

GitHub-only: there is no local working copy whose mtimes can be walked.
GitHub `push` webhooks hand us the changed paths from the payload
(commits[*].added/modified/removed) and we mark the Doc pages watching them
stale — see api/v1/github_webhooks.py.

The unified freshness model: a page is STALE when it needs review — code
moved under its watch_paths since the last review (code_changed_at >
last_reviewed_at, derived, never stored). Stale clears ONLY when the page is
reviewed: a human edit, an accepted DocEdit, or an explicit
confirm_doc_current from a document run that verified the page and found it
still true. Checked stamps are audit-coverage history, not freshness — a
code-quality audit does not clear docs-stale. No LLM is involved here — pure
path matching (domains/repositories/services/path_matching.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from domains.docs.models import Doc
from domains.freshness import mark_reviewed
from domains.repositories.services.path_matching import (
    MAX_STALE_PATHS,
    mark_nodes_stale,
    normalize_path,
    watches_path,
)

__all__ = [
    "MAX_STALE_PATHS",
    "watches_path",
    "docs_watching_paths",
    "mark_docs_stale",
    "confirm_doc_current",
    "DocStaleResult",
]


@dataclass
class DocStaleResult:
    docs_marked: int = 0
    errors: list[str] = field(default_factory=list)


async def docs_watching_paths(repository_uid: str, paths: list[str]) -> list[str]:
    """Doc uids whose watch_paths cover any of the given repository paths.

    Lets a write run pre-load its likely-relevant pages into the first-turn
    briefing (inlined verbatim) before any code has changed — matched off the
    paths the run already knows about (a PR's findings, a ticket's linked
    findings). Returns [] when nothing matches; the briefing index + read_doc
    still cover every other page. Archived pages are excluded.
    """
    norm = [p for p in (normalize_path(p) for p in paths) if p]
    if not norm:
        return []
    out: list[str] = []
    for d in await Doc.nodes.filter(repository_uid=repository_uid, archived=False):
        if any(watches_path(list(d.watch_paths or []), p) for p in norm):
            out.append(d.uid)
    return out


async def mark_docs_stale(
    repository_uid: str,
    changed_paths: list[str],
    now: datetime | None = None,
) -> DocStaleResult:
    """Stamp code_changed_at + accumulate stale_paths on every Doc whose
    watch_paths match a changed path.

    Called from the GitHub push webhook. Best-effort per doc: one bad page
    never blocks the rest. Archived pages are excluded.
    """
    if not [p for p in (normalize_path(p) for p in changed_paths) if p]:
        return DocStaleResult()  # nothing changed — skip the DB scan
    docs = list(
        await Doc.nodes.filter(repository_uid=repository_uid, archived=False)
    )
    marked, errors = await mark_nodes_stale(
        docs, changed_paths, watch_attr="watch_paths", now=now
    )
    return DocStaleResult(docs_marked=marked, errors=errors)


async def confirm_doc_current(repository_uid: str, slug: str) -> Doc | None:
    """A document run verified this page against the code and found it still
    true: stamp the review without an edit. Returns None for unknown slugs."""
    from domains.docs.services.doc_service import get_doc_by_slug, normalize_slug

    d = await get_doc_by_slug(repository_uid, normalize_slug(slug))
    if d is None:
        return None
    mark_reviewed(d, datetime.now(UTC))
    await d.save()
    return d
