"""Area freshness — webhook-driven, the Doc freshness flow ported to the
Area map.

GitHub `push` webhooks hand us the changed paths from the payload and we
mark the Areas whose scope_paths cover them stale — same entry point as
Doc pages (domains/agents/services/event_triggers.refresh_docs_for_change).

The unified freshness model: an area is STALE when it needs review — code
moved under its scope_paths since the last review (code_changed_at >
last_reviewed_at, derived, never stored). Stale clears ONLY when the area is
reviewed: a human edit, an accepted AreaEdit, or an explicit
confirm_area_current from a map/document run that verified the area is still
correctly partitioned. Checked stamps are audit-coverage history, not
freshness — a code-quality audit does not clear area-stale. No LLM is
involved here — pure path matching
(domains/repositories/services/path_matching.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from domains.areas.models import Area
from domains.freshness import mark_reviewed
from domains.repositories.services.path_matching import (
    mark_nodes_stale,
    normalize_path,
)


@dataclass
class AreaStaleResult:
    areas_marked: int = 0
    errors: list[str] = field(default_factory=list)


async def mark_areas_stale(
    repository_uid: str,
    changed_paths: list[str],
    now: datetime | None = None,
) -> AreaStaleResult:
    """Stamp code_changed_at + accumulate stale_paths on every Area whose
    scope_paths match a changed path.

    Called from the GitHub push webhook. Best-effort per area: one bad area
    never blocks the rest.
    """
    if not [p for p in (normalize_path(p) for p in changed_paths) if p]:
        return AreaStaleResult()  # nothing changed — skip the DB scan
    areas = list(await Area.nodes.filter(repository_uid=repository_uid))
    marked, errors = await mark_nodes_stale(
        areas, changed_paths, watch_attr="scope_paths", now=now
    )
    return AreaStaleResult(areas_marked=marked, errors=errors)


async def confirm_area_current(repository_uid: str, key: str) -> Area | None:
    """A run verified this area is still correctly partitioned against the
    current code: stamp the review without an edit. Mirrors
    doc_freshness.confirm_doc_current. Returns None for unknown keys."""
    from domains.areas.services.area_service import get_area_by_key, normalize_key

    a = await get_area_by_key(repository_uid, normalize_key(key))
    if a is None:
        return None
    mark_reviewed(a, datetime.now(UTC))
    await a.save()
    return a
