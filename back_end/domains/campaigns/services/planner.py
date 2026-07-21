"""Campaign planning — pure partition + part-list math, no DB.

`normalize_areas` partitions the repository's real file tree across the
docs' watch scopes under one invariant — EVERY FILE IS OWNED BY EXACTLY
ONE AREA, the doc page with the most specific matching watch prefix —
then right-sizes the result (split oversized, merge tiny same-branch,
sweep uncovered paths into a remainder area). `build_plan` turns areas +
lenses into the campaign's part list per template. Both are pure so the
whole planning surface is unit-testable; campaign_service supplies the
loaded docs/tree/lenses.
"""

from __future__ import annotations

from datetime import datetime

from domains.areas.models import child_key_prefix_of
from domains.docs.services.doc_freshness import watches_path

# Target area size in files: areas above target_max are split by first-level
# subdirectory; adjacent same-branch areas below target_min are merged.
DEFAULT_TARGET_MIN = 50
DEFAULT_TARGET_MAX = 150

REMAINDER_TITLE = "Uncovered paths"


def _normalize(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./").rstrip("/")


def _area(
    title: str,
    scope_paths: list[str],
    doc_uids: list[str],
    file_count: int | None,
) -> dict:
    return {
        "title": title,
        "scope_paths": scope_paths,
        "doc_uids": doc_uids,
        "file_count": file_count,
    }


def _doc_watch(d: dict) -> list[str]:
    """A doc's normalized, deduped watch prefixes (order preserved)."""
    return list(
        dict.fromkeys(
            p for p in (_normalize(p) for p in (d.get("watch_paths") or [])) if p
        )
    )


def _doc_slug(d: dict) -> str:
    return str(d.get("slug") or "")


def _slug_segment(d: dict) -> str:
    """The doc's slug branch — only same-branch areas may merge. Slugless
    docs (top-level pages like "conventions") count as their own segment."""
    slug = _doc_slug(d) or str(d.get("uid") or "") or str(d.get("title") or "")
    return slug.split("/", 1)[0]


def _split_by_subdir(area: dict, matched: list[str]) -> list[dict]:
    """Split an oversized area by first-level subdirectory beneath each of
    its scope prefixes. Files sitting directly under a prefix stay grouped
    under the prefix itself."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for path in matched:
        prefix = next(
            (
                w
                for w in area["scope_paths"]
                if path == w or path.startswith(w + "/")
            ),
            "",
        )
        rest = path[len(prefix) :].lstrip("/") if prefix else path
        if prefix and "/" in rest:
            key = f"{prefix}/{rest.split('/', 1)[0]}"
        else:
            key = prefix or path.split("/", 1)[0]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(path)
    return [
        _area(
            f"{area['title']} — {key.rsplit('/', 1)[-1]}",
            [key],
            list(area["doc_uids"]),
            len(groups[key]),
        )
        for key in sorted(order)
    ]


def _merge_tiny(areas: list[dict], *, target_min: int) -> list[dict]:
    """Merge runs of adjacent tiny areas (< target_min files) into one part.

    Only areas from the same slug branch merge (matching "_seg" annotation) —
    "backend/x" may merge with "backend/y", never with "frontend/y". Areas
    without a file count (tree unavailable) are never merged."""
    out: list[dict] = []
    buffer: list[dict] = []

    def _flush() -> None:
        if not buffer:
            return
        if len(buffer) == 1:
            out.append(buffer[0])
        else:
            merged = _area(
                " + ".join(a["title"] for a in buffer),
                [p for a in buffer for p in a["scope_paths"]],
                list(dict.fromkeys(u for a in buffer for u in a["doc_uids"])),
                sum(a["file_count"] or 0 for a in buffer),
            )
            merged["_seg"] = buffer[0].get("_seg", "")
            out.append(merged)
        buffer.clear()

    for area in areas:
        count = area["file_count"]
        if count is None or count >= target_min:
            _flush()
            out.append(area)
            continue
        if buffer and buffer[-1].get("_seg", "") != area.get("_seg", ""):
            _flush()
        buffer.append(area)
        if sum(a["file_count"] or 0 for a in buffer) >= target_min:
            _flush()
    _flush()
    return out


def _exact_scopes(prefix: str, files_under: list[str], owned: set[str]) -> list[str]:
    """A disjoint prefix/file-path set covering exactly the owned files under
    `prefix`. A prefix whose subtree is fully owned is emitted as-is; a shared
    prefix is refined into its first-level children (recursively); owned files
    sitting directly under a shared prefix are listed individually
    (watches_path treats exact paths as matches)."""
    own = [f for f in files_under if f in owned]
    if not own:
        return []
    if len(own) == len(files_under):
        return [prefix]
    out: list[str] = []
    children: dict[str, list[str]] = {}
    for f in files_under:
        rest = f[len(prefix) :].lstrip("/")
        if "/" in rest:
            child = f"{prefix}/{rest.split('/', 1)[0]}"
            children.setdefault(child, []).append(f)
        elif f in owned:
            out.append(f)
    for child, sub in children.items():
        out.extend(_exact_scopes(child, sub, owned))
    return sorted(out)


def normalize_areas(
    docs: list[dict],
    file_paths: list[str],
    *,
    target_min: int = DEFAULT_TARGET_MIN,
    target_max: int = DEFAULT_TARGET_MAX,
) -> list[dict]:
    """Docs' watch scopes → right-sized, NON-OVERLAPPING area dicts against
    the real tree.

    Ownership invariant: every file is owned by exactly one area — the doc
    with the MOST SPECIFIC matching watch prefix. When multiple docs claim
    the identical prefix, the doc with the smallest total claimed file count
    (the most specific page) wins it; ties break to the lexicographically
    first slug. For each file the owning prefix is the longest claimed
    prefix that matches, and the owning doc is that prefix's winner. Each
    doc's area covers exactly the files it owns: its scope_paths are a
    disjoint prefix set, refined down to first-level children (or individual
    file paths) wherever a claimed prefix contains foreign-owned files. Docs
    that end up owning zero files (fully shadowed overview pages) produce no
    area.

    `docs` are {uid, slug, title, watch_paths}; `file_paths` the repo's blob
    paths. Prefix matching reuses doc_freshness.watches_path. When the tree
    is unavailable (empty file_paths) every watched doc becomes one area
    with file_count=None and no remainder — sizing degrades, but the
    identical-prefix dedup still applies (lexicographic-slug winner, since
    claim sizes are unknowable) so degraded plans don't multi-audit either.
    """
    watched = [d for d in docs if _doc_watch(d)]

    # (a) Claims: prefix → indices of docs watching it. Identical prefixes
    # are collapsed to one winner below.
    claims: dict[str, list[int]] = {}
    for i, d in enumerate(watched):
        for p in _doc_watch(d):
            claims.setdefault(p, []).append(i)

    if not file_paths:
        # Degraded: no file counts — identical-prefix winner falls back to
        # the lexicographically first slug. Docs left with no prefixes
        # produce no area.
        def _degraded_winner(idxs: list[int]) -> int:
            return min(idxs, key=lambda j: (_doc_slug(watched[j]), j))

        out: list[dict] = []
        for i, d in enumerate(watched):
            won = [p for p in _doc_watch(d) if _degraded_winner(claims[p]) == i]
            if not won:
                continue
            out.append(
                _area(
                    str(d.get("title") or d.get("slug") or ""),
                    won,
                    [str(d["uid"])] if d.get("uid") else [],
                    None,
                )
            )
        return out

    paths = [p for p in (_normalize(p) for p in file_paths) if p]

    # Identical-prefix winner: the doc whose TOTAL claim is smallest (most
    # specific page); tie → lexicographically first slug.
    claim_count = [
        sum(1 for p in paths if watches_path(_doc_watch(d), p)) for d in watched
    ]
    prefix_winner = {
        prefix: min(
            idxs, key=lambda j: (claim_count[j], _doc_slug(watched[j]), j)
        )
        for prefix, idxs in claims.items()
    }

    # (b) File ownership: the longest matching claimed prefix owns the file;
    # the owning doc is that prefix's winner.
    prefixes_by_len = sorted(claims, key=len, reverse=True)
    owned: list[list[str]] = [[] for _ in watched]
    covered: set[str] = set()
    for f in paths:
        best = next(
            (p for p in prefixes_by_len if f == p or f.startswith(p + "/")), None
        )
        if best is None:
            continue
        owned[prefix_winner[best]].append(f)
        covered.add(f)

    # (c) One area per doc that owns files, scoped by an exact disjoint
    # prefix set; (d) oversized areas split by first-level subdir.
    slugged: list[tuple[str, dict]] = []
    for i, d in enumerate(watched):
        if not owned[i]:
            continue
        own_set = set(owned[i])
        won = [p for p in _doc_watch(d) if prefix_winner[p] == i]
        # Drop won prefixes nested under another won prefix of the same doc:
        # the outer one already covers them, keeping scope_paths disjoint.
        maximal = [
            p
            for p in won
            if not any(
                q != p and (p == q or p.startswith(q + "/")) for q in won
            )
        ]
        scope: list[str] = []
        for p in maximal:
            under = [f for f in paths if f == p or f.startswith(p + "/")]
            scope.extend(_exact_scopes(p, under, own_set))
        area = _area(
            str(d.get("title") or d.get("slug") or ""),
            sorted(scope),
            [str(d["uid"])] if d.get("uid") else [],
            len(owned[i]),
        )
        seg = _slug_segment(d)
        if len(owned[i]) > target_max:
            for piece in _split_by_subdir(area, owned[i]):
                piece["_seg"] = seg
                slugged.append((_doc_slug(d), piece))
        else:
            area["_seg"] = seg
            slugged.append((_doc_slug(d), area))

    # (e) Sort by slug so branch-mates sit adjacent, then merge tiny runs
    # within the same slug branch only.
    slugged.sort(key=lambda pair: pair[0])
    areas = _merge_tiny([a for _, a in slugged], target_min=target_min)

    # (f) Remainder: files no doc watches.
    uncovered = [p for p in paths if p not in covered]
    if uncovered:
        remainder = _area(
            REMAINDER_TITLE,
            sorted({p.split("/", 1)[0] for p in uncovered}),
            [],
            len(uncovered),
        )
        if len(uncovered) > target_max:
            areas.extend(
                _merge_tiny(
                    _split_by_subdir(
                        _area(REMAINDER_TITLE, remainder["scope_paths"], [], len(uncovered)),
                        uncovered,
                    ),
                    target_min=target_min,
                )
            )
        else:
            areas.append(remainder)
    for a in areas:
        a.pop("_seg", None)
    return areas


def areas_from_map(
    subsystem_leaves: list[dict],
    ignore_scopes: list[str],
    file_paths: list[str],
    *,
    target_max: int = DEFAULT_TARGET_MAX,
) -> list[dict]:
    """Area-map subsystem leaves → area dicts sized against the real tree.

    Leaves are {area_key, title, scope_paths, doc_uids} from the enabled
    Area map. Unlike normalize_areas, leaves are NEVER auto-split or
    tiny-merged — semantic sizing is the mapping agent's job; an oversized
    leaf is only FLAGGED (`oversized`) so the map can be refined. Each
    output dict carries its `area_key`.

    Ignore scopes are subtracted from leaf counts and the remainder —
    non-auditable files get no run scoped to them, even when a leaf scope
    also covers them.

    The remainder — files matched by no leaf scope and no ignore scope —
    has no semantic owner, so it keeps the mechanical split/merge treatment
    of the docs planner. Remainder areas carry area_key "". When the tree
    is unavailable (empty file_paths) leaves pass through with
    file_count=None and no remainder exists.
    """
    paths = [p for p in (_normalize(p) for p in file_paths) if p]
    ignores = [p for p in (_normalize(p) for p in ignore_scopes) if p]

    out: list[dict] = []
    covered: set[str] = set()
    for leaf in subsystem_leaves:
        scope = [
            p for p in (_normalize(p) for p in (leaf.get("scope_paths") or [])) if p
        ]
        count: int | None = None
        if paths:
            matched = [
                f for f in paths if watches_path(scope, f) and not watches_path(ignores, f)
            ]
            covered.update(matched)
            count = len(matched)
        area = _area(
            str(leaf.get("title") or leaf.get("area_key") or ""),
            scope,
            list(leaf.get("doc_uids") or []),
            count,
        )
        area["area_key"] = str(leaf.get("area_key") or "")
        area["oversized"] = bool(count and count > target_max)
        out.append(area)

    uncovered = [
        f for f in paths if f not in covered and not watches_path(ignores, f)
    ]
    if uncovered:
        remainder = _area(
            REMAINDER_TITLE,
            sorted({p.split("/", 1)[0] for p in uncovered}),
            [],
            len(uncovered),
        )
        if len(uncovered) > target_max:
            pieces = _merge_tiny(
                _split_by_subdir(remainder, uncovered),
                target_min=DEFAULT_TARGET_MIN,
            )
        else:
            pieces = [remainder]
        for piece in pieces:
            piece.pop("_seg", None)
            piece["area_key"] = ""
            piece["oversized"] = bool(
                piece["file_count"] and piece["file_count"] > target_max
            )
        out.extend(pieces)
    return out


def filter_by_prefix(areas: list[dict], area_prefix: str) -> list[dict]:
    """The areas at or under `area_prefix` in the key hierarchy. Pure.

    Empty prefix keeps everything. Areas with an empty area_key
    (docs-derived, remainder) have no place in the hierarchy, so they only
    survive an empty prefix. Boundary via child_key_prefix_of — "backend"
    never captures "backend-jobs".
    """
    if not area_prefix:
        return list(areas)
    return [
        a
        for a in areas
        if (key := str(a.get("area_key") or ""))
        and (key == area_prefix or child_key_prefix_of(area_prefix, key))
    ]


def _area_recency(
    area: dict, path_recency: dict[str, datetime | None]
) -> datetime | None:
    """The area's coverage age: for each scope path, the latest covered
    stamp under it; the area scores by its STALEST scope path. None = some
    scope path was never covered (ranks first for rotation)."""
    per_scope: list[datetime | None] = []
    for scope in area.get("scope_paths") or []:
        latest: datetime | None = None
        for path, at in path_recency.items():
            if at is None or not (path == scope or path.startswith(scope + "/")):
                continue
            if latest is None or at > latest:
                latest = at
        per_scope.append(latest)
    if not per_scope or any(v is None for v in per_scope):
        return None
    return min(v for v in per_scope if v is not None)


def _part(idx: int, kind: str, title: str, area: dict | None, lens_keys: list[str]) -> dict:
    return {
        "idx": idx,
        "kind": kind,
        "title": title,
        "scope_paths": list((area or {}).get("scope_paths") or []),
        "doc_uids": list((area or {}).get("doc_uids") or []),
        "lens_keys": list(lens_keys),
        "run_uid": "",
        "state": "pending",
        "file_count": (area or {}).get("file_count"),
        "area_key": str((area or {}).get("area_key") or ""),
    }


def build_plan(
    template: str,
    areas: list[dict],
    lenses: list[dict],
    *,
    k: int = 3,
    path_recency: dict[str, datetime | None] | None = None,
    focus_lens: str | None = None,
    feature_areas: list[dict] | None = None,
) -> list[dict]:
    """Areas + lenses → the campaign's ordered part list.

    - full:     every area (all enabled local lenses) + one feature part per
                `feature_areas` entry (implementation-gaps lens) + one
                global part per enabled global lens.
    - rotation: the k least-recently-covered areas (never-covered first,
                scored by `path_recency` over their scope paths); no globals
                and no feature parts.
    - focused:  every area with just `focus_lens`, plus that lens's global
                sweep when it names a global agent.

    Parts get idx assigned sequentially, areas before features before globals.
    """
    enabled = [dict(lens) for lens in lenses if lens.get("enabled", True)]
    local = [lens for lens in enabled if (lens.get("scope") or "local") == "local"]
    global_ = [lens for lens in enabled if lens.get("scope") == "global"]
    local_keys = [str(lens["key"]) for lens in local]

    def _global_part(lens: dict) -> dict:
        return _part(0, "global", f"Global sweep — {lens['key']}", None, [str(lens["key"])])

    picked: list[dict]
    globals_out: list[dict]
    if template == "rotation":
        recency = path_recency or {}
        scored = [(i, _area_recency(a, recency)) for i, a in enumerate(areas)]
        _EPOCH = datetime.min
        scored.sort(key=lambda pair: (pair[1] is not None, pair[1] or _EPOCH, pair[0]))
        picked = [
            _part(0, "area", areas[i]["title"], areas[i], local_keys)
            for i, _ in scored[: max(k, 0)]
        ]
        globals_out = []
    elif template == "focused":
        focus = str(focus_lens or "")
        picked = [_part(0, "area", a["title"], a, [focus]) for a in areas]
        lens = next((lens for lens in enabled if str(lens.get("key")) == focus), None)
        globals_out = (
            [_global_part(lens)] if lens is not None and lens.get("global_agent_key") else []
        )
    else:  # full
        picked = [_part(0, "area", a["title"], a, local_keys) for a in areas]
        picked += [
            _part(0, "feature", fa["title"], fa, ["implementation-gaps"])
            for fa in (feature_areas or [])
        ]
        globals_out = [_global_part(lens) for lens in global_]

    parts = picked + globals_out
    for idx, part in enumerate(parts):
        part["idx"] = idx
    return parts
