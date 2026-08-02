"""Area health rollup — every upkeep signal for a repository's area map in
ONE pass.

This is what lets the Health page fold into Areas. The signals it merges were
previously either unavailable per-area or only reachable one area at a time:

- REVIEW staleness came from the Area DTO (cheap), but grouping rows could
  never show it — a grouping owns no scope_paths, so a parent read "clean"
  while every leaf under it needed review. Rolled up here.
- AUDIT COVERAGE was reachable only via `checked_service.stamps_for_paths`,
  which rescans every stamp in the repository *per area* — fine for one detail
  page, quadratic for a table.
- DOC freshness lived on a separate page keyed by Doc, with no notion of which
  area a page belongs to.

So the shape of this module is dictated by that: load each collection exactly
once, build two indexes, and answer every area from them.

  * file counts — binary search over the sorted tree instead of scanning it
    per scope ("/" boundary respected via the sentinel below).
  * stamp/doc → area — a scope-path index walked up each path's ancestors,
    so resolution is O(path depth), not O(areas).

Staleness stays DERIVED (`area_is_stale`, code_changed_at > last_reviewed_at).
Nothing here is persisted; adding a stored `stale` flag would create a second
source of truth that drifts from the webhook.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import UTC, datetime

from domains.areas.models import (
    Area,
    area_is_stale,
    child_key_prefix_of,
    is_leaf,
)
from domains.areas.schemas import (
    AreaHealthRowDTO,
    AreaHealthSummaryDTO,
    AreasHealthDTO,
    UnassignedDocDTO,
)
from domains.repositories.services.path_matching import normalize_path

# Kinds that are explicitly NOT audit targets — they exist to close the
# partition (lockfiles, generated code, vendored trees). Counting them in the
# summary tiles would inflate every number with work nobody intends to do.
_NON_AUDITABLE_KINDS = {"ignore", "feature_ignore"}

# The character immediately after "/" in ASCII. Bounds a prefix range so that
# "back_end/domains" matches "back_end/domains/areas.py" but never
# "back_end/domains-legacy/x.py".
_AFTER_SLASH = "0"


def _ancestors(path: str) -> list[str]:
    """The path itself plus every directory above it, nearest first."""
    out = [path]
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        out.append(path)
    return out


def _prune_nested(scopes: list[str]) -> list[str]:
    """Drop scopes contained by another scope of the SAME area.

    Without this an area declaring both "src" and "src/api" would count every
    file under src/api twice.
    """
    kept: list[str] = []
    for s in sorted(set(scopes), key=lambda p: (len(p), p)):
        if any(s == k or s.startswith(k + "/") for k in kept):
            continue
        kept.append(s)
    return kept


def _count_files(sorted_paths: list[str], scope: str) -> int:
    """Files at or under `scope`, via binary search over the sorted tree."""
    if not scope:
        return 0
    lo = bisect_left(sorted_paths, scope + "/")
    hi = bisect_left(sorted_paths, scope + _AFTER_SLASH)
    count = hi - lo
    # A scope may name a single file rather than a directory.
    i = bisect_left(sorted_paths, scope)
    if i < len(sorted_paths) and sorted_paths[i] == scope:
        count += 1
    return count


def _dt(value) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _spec_state(area: Area, stale: bool) -> str:
    if not (area.spec or "").strip():
        return "missing"
    # Mirrors the rule generate-specs already applies (sweep.feature_leaf_spec_targets):
    # a spec written against code that has since moved is not a current spec.
    return "stale" if stale else "present"


async def area_health(repository_uid: str) -> AreasHealthDTO:
    """Every area of a repository with its review / docs / audit state.

    One query per collection, no per-area fan-out.
    """
    from domains.checked.models import Checked
    from domains.docs.models import Doc, doc_is_stale
    from domains.repositories.models import Repository
    from domains.repositories.services.file_tree import file_tree_paths

    areas = list(await Area.nodes.filter(repository_uid=repository_uid))
    docs = [
        d
        for d in await Doc.nodes.filter(repository_uid=repository_uid, archived=False)
    ]
    stamps = list(await Checked.nodes.filter(repository_uid=repository_uid))

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    tree_paths: list[str] = []
    tree_degraded = "repository not found"
    if repo is not None:
        tree_paths, tree_degraded = await file_tree_paths(repo)
    sorted_paths = sorted(tree_paths)

    enabled_keys = [a.key for a in areas if a.enabled]

    # ---- index: scope path → area indices (walked up each path's ancestors)
    scope_index: dict[str, list[int]] = {}
    for idx, a in enumerate(areas):
        for raw in a.scope_paths or []:
            p = normalize_path(str(raw))
            if p:
                scope_index.setdefault(p, []).append(idx)

    def areas_for_path(path: str) -> set[int]:
        hits: set[int] = set()
        p = normalize_path(path)
        if not p:
            return hits
        for anc in _ancestors(p):
            hits.update(scope_index.get(anc, ()))
        return hits

    # ---- audit coverage: latest overlapping stamp per area
    latest_stamp: dict[int, Checked] = {}
    latest_by_scope_uid: dict[str, Checked] = {}
    for c in stamps:
        prev = latest_by_scope_uid.get(c.scope_uid)
        if prev is None or _dt(c.checked_at) > _dt(prev.checked_at):
            latest_by_scope_uid[c.scope_uid] = c
        touched: set[int] = set()
        for path in c.covered_paths or []:
            touched |= areas_for_path(str(path))
        for idx in touched:
            prev_stamp = latest_stamp.get(idx)
            if prev_stamp is None or _dt(c.checked_at) > _dt(prev_stamp.checked_at):
                latest_stamp[idx] = c

    # ---- docs → areas (explicit doc_uids link ∪ watch_path overlap)
    docs_by_area: dict[int, set[str]] = {}
    doc_by_uid = {d.uid: d for d in docs}
    assigned_docs: set[str] = set()
    for idx, a in enumerate(areas):
        for doc_uid in a.doc_uids or []:
            if str(doc_uid) in doc_by_uid:
                docs_by_area.setdefault(idx, set()).add(str(doc_uid))
                assigned_docs.add(str(doc_uid))
    for d in docs:
        hits: set[int] = set()
        for wp in d.watch_paths or []:
            hits |= areas_for_path(str(wp))
        for idx in hits:
            docs_by_area.setdefault(idx, set()).add(d.uid)
            assigned_docs.add(d.uid)

    # ---- pending edits per area (one scan, not one query per area)
    from domains.areas.models import AreaEdit

    pending_by_area: dict[str, int] = {}
    for e in await AreaEdit.nodes.filter(
        repository_uid=repository_uid, status="pending"
    ):
        if e.area_uid:
            pending_by_area[e.area_uid] = pending_by_area.get(e.area_uid, 0) + 1

    # ---- rows
    stale_flags = [area_is_stale(a) for a in areas]
    rows: list[AreaHealthRowDTO] = []
    for idx, a in enumerate(areas):
        stale = stale_flags[idx]
        leaf = is_leaf(a.key, enabled_keys)
        scopes = _prune_nested(
            [normalize_path(str(p)) for p in (a.scope_paths or []) if p]
        )
        file_count = (
            sum(_count_files(sorted_paths, s) for s in scopes) if sorted_paths else None
        )

        # A grouping owns no paths and so can never itself be stale; surface
        # its children's state instead of a misleading clean row.
        stale_descendants = sum(
            1
            for j, other in enumerate(areas)
            if stale_flags[j] and child_key_prefix_of(a.key, other.key)
        )

        doc_uids = docs_by_area.get(idx, set())
        stamp = latest_stamp.get(idx)
        rows.append(
            AreaHealthRowDTO(
                uid=a.uid,
                key=a.key,
                kind=a.kind or "subsystem",
                title=a.title or "",
                enabled=bool(a.enabled),
                is_leaf=leaf,
                depth=a.key.count("/"),
                scope_paths=[str(p) for p in (a.scope_paths or []) if p],
                file_count=file_count,
                stale=stale,
                stale_paths=[str(p) for p in (a.stale_paths or []) if p],
                stale_descendants=stale_descendants,
                code_changed_at=a.code_changed_at,
                last_reviewed_at=a.last_reviewed_at,
                pending_edits=pending_by_area.get(a.uid, 0),
                docs_total=len(doc_uids),
                docs_stale=sum(
                    1
                    for u in doc_uids
                    if u in doc_by_uid and doc_is_stale(doc_by_uid[u])
                ),
                spec_state=_spec_state(a, stale),
                last_checked=stamp.checked_at if stamp else None,
                outcome=(stamp.outcome or "") if stamp else "",
                revision=(stamp.revision or "") if stamp else "",
                coverage_source=(stamp.coverage_source or "unknown") if stamp else "unknown",
            )
        )

    rows.sort(key=lambda r: r.key)

    # ---- docs no area covers — they'd vanish when Health folds into Areas
    unassigned = []
    for d in docs:
        if d.uid in assigned_docs:
            continue
        c = latest_by_scope_uid.get(d.uid)
        unassigned.append(
            UnassignedDocDTO(
                uid=d.uid,
                slug=d.slug or "",
                title=d.title or "",
                stale=doc_is_stale(d),
                last_checked=c.checked_at if c else None,
                outcome=(c.outcome or "") if c else "",
            )
        )
    unassigned.sort(key=lambda d: (not d.stale, d.slug))

    # ---- summary tiles over auditable leaves only
    auditable = [
        r
        for r in rows
        if r.enabled and r.is_leaf and r.kind not in _NON_AUDITABLE_KINDS
    ]
    # The tiles count ANY stamp as coverage, including inferred ones — the row
    # carries coverage_source so a reader can see the difference, but redefining
    # `fresh`/`never_audited` around it would reclassify every historical stamp
    # (m0022 backfills them all to "unknown") and animate Fresh to ~0 on the
    # first load after deploy, for no actionable gain.
    summary = AreaHealthSummaryDTO(
        total=len(auditable),
        stale=sum(1 for r in auditable if r.stale),
        never_audited=sum(1 for r in auditable if r.last_checked is None),
        fresh=sum(1 for r in auditable if not r.stale and r.last_checked is not None),
    )

    return AreasHealthDTO(
        repository_uid=repository_uid,
        rows=rows,
        unassigned_docs=unassigned,
        summary=summary,
        tree_degraded=tree_degraded,
        freshness_synced_at=getattr(repo, "freshness_synced_at", None),
        freshness_degraded_reason=getattr(repo, "freshness_degraded_reason", "") or "",
    )
