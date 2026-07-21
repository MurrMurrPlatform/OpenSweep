"""Area freshness — webhook-driven, the Doc freshness flow ported to the
Area map.

GitHub `push` webhooks hand us the changed paths from the payload and we
mark the Areas whose scope_paths cover them stale — same entry point as
Doc pages (domains/agents/services/event_triggers.refresh_docs_for_change).

Staleness is derived (code_changed_at > last_reviewed_at), never stored.
It clears when the area is reviewed: a human edit or an accepted AreaEdit.
No LLM is involved here — pure path matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from domains.areas.models import Area
from domains.docs.services.doc_freshness import watches_path
from logging_config import logger

# stale_paths is briefing material, not a changelog — keep it bounded.
_MAX_STALE_PATHS = 200


@dataclass
class AreaStaleResult:
    areas_marked: int = 0
    errors: list[str] = field(default_factory=list)


def _normalize(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./").rstrip("/")


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
    result = AreaStaleResult()
    changed = [p for p in (_normalize(p) for p in changed_paths) if p]
    if not changed:
        return result
    now = now or datetime.now(UTC)

    areas = [a for a in await Area.nodes.all() if a.repository_uid == repository_uid]
    for a in areas:
        try:
            hits = [p for p in changed if watches_path(list(a.scope_paths or []), p)]
            if not hits:
                continue
            a.code_changed_at = now
            merged = list(dict.fromkeys(list(a.stale_paths or []) + hits))
            a.stale_paths = merged[:_MAX_STALE_PATHS]
            await a.save()
            result.areas_marked += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"area={a.uid}: {type(exc).__name__}: {exc}"
            logger.warning(f"area freshness: {msg}")
            result.errors.append(msg)
    return result
