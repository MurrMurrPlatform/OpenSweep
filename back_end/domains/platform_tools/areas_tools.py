"""Platform tool: propose_area_edit — the agent's write path into the
Area map.

Area edits write platform state, not repository state, so the tool is
tracking-safe in every playbook including analyze-only — no change dial
involved: proposals always land as pending AreaEdits for human review.
"""

from __future__ import annotations

import time
from typing import Any

from domains.areas.services import area_coverage, area_freshness, area_service
from logging_config import logger

# A mapping run calls propose_area_edit once per area — dozens of times over a
# few minutes — and the file tree it is measured against does not move during
# a run. Fetching the tree per call would put a provider round-trip in front of
# every proposal, so hold it briefly in process.
_TREE_TTL_SECONDS = 120
_TREE_CACHE_MAX_REPOS = 32  # a worker serves few repos at once; never grow forever
_tree_cache: dict[str, tuple[float, list[str]]] = {}


async def _tracked_files(repository_uid: str) -> list[str]:
    """The repository's file list for coverage, or [] when unavailable."""
    hit = _tree_cache.get(repository_uid)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _TREE_TTL_SECONDS:
        return hit[1]

    from domains.repositories.models import Repository
    from domains.repositories.services.file_tree import file_tree_paths

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        return []
    paths, _degraded = await file_tree_paths(repo)
    # A truncated tree still measures coverage honestly (against a partial
    # universe); an empty one means "unknown", and unknown must not warn.
    if len(_tree_cache) >= _TREE_CACHE_MAX_REPOS:
        _tree_cache.clear()
    _tree_cache[repository_uid] = (now, paths)
    return paths


async def _axis_gap_warning(
    *, repository_uid: str, kind: str, source_run_uid: str
) -> str:
    """How much of the proposed-into axis is still unclaimed, as one sentence
    — or "" when the axis is whole (or coverage can't be computed).

    Advisory, exactly like the overlap warnings: the agent otherwise only
    learns about its collisions and never about the half of the tree it
    forgot. Measured over the map as THIS run would leave it — the accepted
    areas plus its own pending proposals."""
    axis = area_coverage.axis_of_kind(kind)
    if not axis:
        return ""
    tracked = await _tracked_files(repository_uid)
    if not tracked:
        return ""

    from domains.areas.models import Area, AreaEdit

    rows: dict[str, dict] = {}
    for a in await Area.nodes.filter(repository_uid=repository_uid):
        rows[a.key] = {
            "key": a.key,
            "kind": a.kind or "subsystem",
            "scope_paths": list(a.scope_paths or []),
            "enabled": bool(a.enabled),
        }
    if source_run_uid:
        for e in await AreaEdit.nodes.filter(
            repository_uid=repository_uid,
            status="pending",
            source_run_uid=source_run_uid,
        ):
            rows[e.key or ""] = {
                "key": e.key or "",
                "kind": e.kind or "subsystem",
                "scope_paths": list(e.scope_paths or []),
                "enabled": bool(getattr(e, "proposed_enabled", True)),
            }

    result = area_coverage.axis_coverage(tracked, list(rows.values()))[axis]
    if result["complete"]:
        return ""
    return (
        area_coverage.gap_sentence(axis, result)
        + f" — keep proposing {axis} areas until every tracked file is claimed"
    )


async def propose_area_edit(
    *,
    repository_uid: str,
    key: str,
    kind: str = "subsystem",
    title: str = "",
    scope_paths: list[str] | None = None,
    spec: str = "",
    rationale: str = "",
    doc_uids: list[str] | None = None,
    enabled: bool = True,
    source_run_uid: str = "",
    # The dispatcher/envelope injects `executor` (and other scope keys) on
    # every tool call uniformly — absorb it via **_; this tool doesn't use it.
    **_: Any,
) -> dict[str, Any]:
    """Propose one Area for the repository's area map. Kinds, on two
    independent axes that each partition the whole repository: "subsystem"
    (every auditable file belongs to exactly one subsystem leaf) with
    "ignore" for non-auditable files (the spec must state WHY), and
    "feature" (spec-anchored end-to-end flow) with "feature_ignore" for
    paths that implement no feature (lockfiles, generated code, static
    assets — the spec must state WHY). Keys are path-like —
    "backend/delivery/convergence" nests under backend/delivery, and files
    belong to leaf areas only. An unknown key proposes a new area; an
    existing key proposes a full replacement of it (kind, title,
    scope_paths, spec, doc_uids). Lands as a pending edit for human review —
    keep specs small and set scope_paths to the repository paths the area
    covers so code changes there mark it for review. Pass enabled=false to
    propose RETIRING an area that should no longer exist. The result's
    `warnings` list the partition conflicts your proposal would create
    (against the live map and your own earlier proposals this run) AND how
    much of the axis you just proposed into is still unclaimed — resolve
    both by re-proposing before you finish."""
    result = await area_service.propose_area_edit(
        repository_uid=repository_uid,
        proposed_spec=spec,
        rationale=rationale,
        key=key,
        kind=kind,
        enabled=enabled,
        title=title,
        scope_paths=scope_paths,
        doc_uids=doc_uids,
        source_run_uid=source_run_uid or "",
    )
    try:
        gap = await _axis_gap_warning(
            repository_uid=repository_uid,
            kind=kind,
            source_run_uid=source_run_uid or "",
        )
    except Exception as exc:  # noqa: BLE001 — advisory: never fail the proposal
        logger.warning(
            f"propose_area_edit {key}: coverage warning skipped "
            f"({type(exc).__name__}: {exc})",
            extra={"tag": "areas"},
        )
        gap = ""
    if gap:
        result["warnings"] = list(result.get("warnings") or []) + [gap]
    return result


async def confirm_area_current(
    *,
    repository_uid: str,
    key: str,
    **_: Any,
) -> dict[str, Any]:
    """You verified this area against the current code and it needs no edit:
    stamp the review so its stale flag clears.

    This covers the WHOLE area, not just the partition: its scope_paths and
    kind still hold AND — if it has one — its spec still describes what the
    code does. There is one review stamp per area, so confirming also marks
    the spec current and drops the area from the generate-specs refresh queue.
    Do not call it after checking only the partition of a spec'd area; propose
    an edit or leave it stale instead. Never call it to silence a stale marker
    you did not verify."""
    a = await area_freshness.confirm_area_current(repository_uid, key)
    if a is None:
        return {"status": "not_found", "key": key}
    return {"status": "ok", "key": a.key}
