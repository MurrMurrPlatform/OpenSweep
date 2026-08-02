"""Shared path-matching + webhook staleness helpers.

The single home for the pure path math both Doc freshness and Area freshness
lean on: normalize a repository path, test whether a set of watch/scope paths
covers a changed path, and stamp the matching nodes stale from a GitHub push.

Staleness is derived (code_changed_at > last_reviewed_at), never stored. This
module only stamps `code_changed_at` and accumulates `stale_paths`; the review
side (edit / accepted edit / confirm_*_current) advances `last_reviewed_at`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from logging_config import logger

# stale_paths is briefing material, not a changelog — keep it bounded. The cap
# drops the OLDEST entries: a reviewer's question is "what changed since I last
# looked", which the most recent paths answer. Keeping the oldest instead froze
# a busy node's list at its first 200 paths forever, so the tooltip and the run
# briefing showed stale history and never the change that triggered the badge.
MAX_STALE_PATHS = 200


def normalize_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./").rstrip("/")


def watches_path(watch_paths: list[str], changed: str) -> bool:
    """True when `changed` equals a watched path or lives under a watched dir."""
    for raw in watch_paths:
        watched = normalize_path(raw)
        if not watched:
            continue
        if changed == watched or changed.startswith(watched + "/"):
            return True
    return False


async def mark_nodes_stale(
    nodes: Sequence,
    changed_paths: list[str],
    *,
    watch_attr: str,
    now: datetime | None = None,
) -> tuple[int, list[str]]:
    """Stamp `code_changed_at` + accumulate `stale_paths` on every node whose
    `watch_attr` paths match a changed path.

    Best-effort per node: one bad node never blocks the rest. Returns
    (marked_count, errors). `changed_paths` are normalized here; callers may
    pass raw push paths.
    """
    errors: list[str] = []
    changed = [p for p in (normalize_path(p) for p in changed_paths) if p]
    if not changed:
        return 0, errors
    now = now or datetime.now(UTC)

    marked = 0
    for node in nodes:
        try:
            watched = list(getattr(node, watch_attr, None) or [])
            hits = [p for p in changed if watches_path(watched, p)]
            if not hits:
                continue
            node.code_changed_at = now
            # Newest first, so the cap sheds history rather than the change that
            # just landed. A re-touched path is promoted rather than duplicated.
            merged = list(dict.fromkeys(hits + list(node.stale_paths or [])))
            node.stale_paths = merged[:MAX_STALE_PATHS]
            await node.save()
            marked += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"{getattr(node, 'uid', '?')}: {type(exc).__name__}: {exc}"
            logger.warning(f"path_matching: mark_nodes_stale: {msg}")
            errors.append(msg)
    return marked, errors
