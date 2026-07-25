"""Axis coverage — do the map's two axes actually PARTITION the repository?

The Area map carves a repository along TWO independent axes:

  * the SUBSYSTEM axis — kinds `subsystem` + `ignore`
  * the FEATURE axis   — kinds `feature` + `feature_ignore`

Each axis is a partition on its own: every tracked file belongs to exactly
one LEAF per axis. `area_service` answers "do these areas COLLIDE?"; this
module answers the other half — "which files does no leaf claim?".

The axes are computed independently because that is exactly how the map
breaks: a mapping run once produced a complete subsystem partition (~24
areas) plus 5 feature areas covering a single directory, then declared
victory. A complete subsystem axis must never make an incomplete feature
axis look finished.

Pure — plain lists/dicts in, a plain dict out. No Neo4j, no git, no I/O, so
callers (the propose tool, the executor's continuation gate) can be tested
without either.

NOT the same thing as `area_freshness` / `AreaCoverageDTO`: those mean
audit-RECENCY ("when was this area last checked"), not file coverage.
"""

from __future__ import annotations

from domains.repositories.services.path_matching import normalize_path, watches_path

# Which kinds live on which axis. An `ignore` area covers the subsystem axis
# (non-auditable files still have to belong somewhere), `feature_ignore` does
# the same for the feature axis — lockfiles and generated assets implement no
# product feature, and pretending otherwise is what leaves the axis unfinished.
SUBSYSTEM_AXIS_KINDS = frozenset({"subsystem", "ignore"})
FEATURE_AXIS_KINDS = frozenset({"feature", "feature_ignore"})

AXES: dict[str, frozenset[str]] = {
    "subsystem": SUBSYSTEM_AXIS_KINDS,
    "feature": FEATURE_AXIS_KINDS,
}

# An unmapped repository would otherwise return one entry per tracked file;
# the lists are evidence for a human/agent, the totals are the truth.
MAX_REPORTED_PATHS = 200

# How many paths a one-line gap sentence names before "…".
GAP_SAMPLE = 8


def axis_of_kind(kind: str) -> str:
    """Which axis a kind partitions; "" for kinds that partition neither."""
    for axis, kinds in AXES.items():
        if (kind or "") in kinds:
            return axis
    return ""


def axis_coverage(
    tracked_files: list[str],
    areas: list[dict],
    *,
    cap: int = MAX_REPORTED_PATHS,
) -> dict:
    """Per-axis file coverage of `tracked_files` by `areas`.

    `areas` rows carry at least {key, kind, scope_paths, enabled} — the same
    shape `area_service.validate_area_fields` takes, so accepted Areas and
    pending AreaEdits can be mixed freely.

    Only LEAVES contribute coverage: an enabled area whose `scope_paths` is
    empty is a scopeless grouping parent ("features", "backend") that owns no
    files by design. Disabled areas are out of the partition entirely.

    A file is covered when one of an area's scope_paths equals it or is a
    "/"-boundary directory prefix of it — the same rule `_paths_overlap`
    (and doc freshness) is built on, via `watches_path`.

    Returns, for each axis independently::

        {"subsystem": {
            "complete": bool,          # nothing uncovered on this axis
            "leaves": int,             # scoped, enabled areas on the axis
            "tracked_total": int,      # distinct tracked files considered
            "uncovered": [path, ...],  # capped, sorted
            "uncovered_total": int,    # ALWAYS the true count
            "overlapping": [{"path": str, "keys": [key, ...]}, ...],  # capped
            "overlapping_total": int,  # ALWAYS the true count
         },
         "feature": {...same shape...}}

    `complete` tracks GAPS only. Overlaps are reported but do not flip it:
    colliding areas already have their own warning path in `area_service`,
    and a collision is a fixable conflict rather than a missing partition.
    """
    files = sorted({p for p in (normalize_path(f) for f in tracked_files or []) if p})

    out: dict[str, dict] = {}
    for axis, kinds in AXES.items():
        leaves: list[tuple[str, list[str]]] = []
        for area in areas or []:
            if (area.get("kind") or "subsystem") not in kinds:
                continue
            if not area.get("enabled", True):
                continue
            scopes = [
                p
                for p in (normalize_path(str(s)) for s in area.get("scope_paths") or [])
                if p
            ]
            if not scopes:
                continue  # scopeless grouping parent — owns no files
            leaves.append((str(area.get("key") or ""), scopes))

        uncovered: list[str] = []
        overlapping: list[dict] = []
        for path in files:
            owners = [key for key, scopes in leaves if watches_path(scopes, path)]
            if not owners:
                uncovered.append(path)
            elif len(owners) > 1:
                overlapping.append({"path": path, "keys": sorted(set(owners))})

        out[axis] = {
            "complete": not uncovered,
            "leaves": len(leaves),
            "tracked_total": len(files),
            "uncovered": uncovered[:cap],
            "uncovered_total": len(uncovered),
            "overlapping": overlapping[:cap],
            "overlapping_total": len(overlapping),
        }
    return out


def incomplete_axes(coverage: dict) -> list[str]:
    """Axis names with at least one uncovered file, in AXES order."""
    return [a for a in AXES if not (coverage.get(a) or {}).get("complete", True)]


def gap_sentence(axis: str, result: dict, *, sample: int = GAP_SAMPLE) -> str:
    """One line naming the size of an axis's gap and a few of its paths.

    Shared by the propose-time warning and the executor's continuation
    prompt so the agent reads the same sentence in both places."""
    total = int(result.get("uncovered_total") or 0)
    if not total:
        return ""
    paths = list(result.get("uncovered") or [])[:sample]
    more = f" (+{total - len(paths)} more)" if total > len(paths) else ""
    return (
        f"{axis} axis incomplete: {total} of {result.get('tracked_total', 0)} "
        f"tracked files belong to no {axis} leaf — e.g. "
        + ", ".join(paths)
        + more
    )
