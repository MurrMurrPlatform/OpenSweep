"""Campaign planning — pure partition + part-list math, no DB.

`normalize_areas` partitions the repository's real file tree across the
docs' watch scopes under one invariant — EVERY FILE IS OWNED BY EXACTLY
ONE AREA, the doc page with the most specific matching watch prefix —
then right-sizes the result (split oversized, merge tiny same-branch,
sweep uncovered paths into a remainder area). `build_plan_by_kind` turns
areas + lenses into the campaign's part list per kind. Both are pure so the
whole planning surface is unit-testable; campaign_service supplies the
loaded docs/tree/lenses.
"""

from __future__ import annotations

from datetime import datetime

from domains.areas.models import child_key_prefix_of
from domains.repositories.services.path_matching import watches_path

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
    Each area from that degraded path carries `degraded=True` so the plan and
    the dispatched run can surface that the partition was guessed, not
    file-owned — never silently reported as a reliable partition.
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
            area = _area(
                str(d.get("title") or d.get("slug") or ""),
                won,
                [str(d["uid"])] if d.get("uid") else [],
                None,
            )
            # The tree was unavailable — this partition is a lexicographic
            # guess, not a real file-owned split. Mark every area so the plan
            # (and the run) can surface that it ran against a guessed
            # partition rather than silently reporting success.
            area["degraded"] = True
            out.append(area)
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
) -> tuple[list[dict], dict]:
    """Area-map subsystem leaves → (area dicts sized against the real tree,
    partition health).

    Leaves are {area_key, title, scope_paths, doc_uids} from the enabled
    Area map. Unlike normalize_areas, leaves are NEVER auto-split or
    tiny-merged — semantic sizing is the mapping agent's job; an oversized
    leaf is only FLAGGED (`oversized`) so the map can be refined. Each
    output dict carries its `area_key` plus `dead_scope_paths` — its scope
    entries matching zero tree files ([] when the tree is empty).

    Ignore scopes are subtracted from leaf counts and the remainder —
    non-auditable files get no run scoped to them, even when a leaf scope
    also covers them.

    Health is {"overlapping_files": files claimed by more than one leaf
    (sum of per-leaf counts minus the distinct covered set),
    "dead_ignore_scopes": ignore scope entries matching no tree files} —
    both zero/empty when the tree is empty.

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
    claimed = 0
    for leaf in subsystem_leaves:
        scope = [
            p for p in (_normalize(p) for p in (leaf.get("scope_paths") or [])) if p
        ]
        count: int | None = None
        dead_scopes: list[str] = []
        if paths:
            matched = [
                f for f in paths if watches_path(scope, f) and not watches_path(ignores, f)
            ]
            covered.update(matched)
            count = len(matched)
            claimed += count
            dead_scopes = [
                s for s in scope if not any(watches_path([s], f) for f in paths)
            ]
        area = _area(
            str(leaf.get("title") or leaf.get("area_key") or ""),
            scope,
            list(leaf.get("doc_uids") or []),
            count,
        )
        area["area_key"] = str(leaf.get("area_key") or "")
        area["oversized"] = bool(count and count > target_max)
        area["dead_scope_paths"] = dead_scopes
        out.append(area)

    health = {
        "overlapping_files": max(claimed - len(covered), 0),
        "dead_ignore_scopes": (
            [s for s in ignores if not any(watches_path([s], f) for f in paths)]
            if paths
            else []
        ),
    }

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
            piece["dead_scope_paths"] = []
        out.extend(pieces)
    return out, health


def bundle_siblings(
    areas: list[dict],
    *,
    target_min: int = DEFAULT_TARGET_MIN,
    target_max: int = DEFAULT_TARGET_MAX,
) -> list[dict]:
    """Group undersized SIBLING map leaves into shared parts — pure.

    Input: areas_from_map subsystem dicts (area_key/title/scope_paths/
    doc_uids/file_count/oversized). Leaves group by parent key prefix (the
    key minus its last segment; top-level keys group under ""); within a
    group, leaves under target_min files greedily merge into a running
    bundle that flushes once it reaches target_min. An area at or above
    target_min always stands alone, a merge never pushes a bundle past
    target_max, and the parent boundary is never crossed — "backend/api"
    and "frontend/api" never share a part. Leaves without a file count
    (degraded tree) and remainder areas (empty area_key) are never bundled.

    Every output dict carries `area_keys` (the bundled keys; [key] for a
    lone leaf, [] for remainders) instead of the singular `area_key`.
    Multi-leaf bundles union scope_paths and doc_uids, sum file_count, and
    title as "<Parent> — <leaf titles joined ' + '>".
    """

    def _passthrough(area: dict, keys: list[str]) -> dict:
        out = {k: v for k, v in area.items() if k != "area_key"}
        out["area_keys"] = keys
        return out

    def _bundle(group: list[dict], parent: str) -> dict:
        keys = [str(a["area_key"]) for a in group]
        label_source = parent or keys[0].split("/", 1)[0]
        label = "/".join(
            seg.replace("-", " ").title() for seg in label_source.split("/")
        )
        return {
            "area_keys": keys,
            "title": f"{label} — " + " + ".join(str(a["title"]) for a in group),
            "scope_paths": list(
                dict.fromkeys(p for a in group for p in (a.get("scope_paths") or []))
            ),
            "doc_uids": list(
                dict.fromkeys(u for a in group for u in (a.get("doc_uids") or []))
            ),
            "file_count": sum(int(a["file_count"] or 0) for a in group),
            "oversized": False,
        }

    out: list[dict] = []
    buffers: dict[str, list[dict]] = {}

    def _flush(parent: str) -> None:
        buffer = buffers.get(parent) or []
        if not buffer:
            return
        if len(buffer) == 1:
            a = buffer[0]
            out.append(_passthrough(a, [str(a["area_key"])]))
        else:
            out.append(_bundle(buffer, parent))
        buffer.clear()

    for area in areas:
        key = str(area.get("area_key") or "")
        count = area.get("file_count")
        if not key:  # remainder pieces have no area — never bundled
            out.append(_passthrough(area, []))
            continue
        if count is None or count >= target_min:
            # Adequate (or unsized) leaves always stand alone.
            out.append(_passthrough(area, [key]))
            continue
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        buffer = buffers.setdefault(parent, [])
        if buffer and sum(int(a["file_count"] or 0) for a in buffer) + count > target_max:
            _flush(parent)
        buffer.append(area)
        if sum(int(a["file_count"] or 0) for a in buffer) >= target_min:
            _flush(parent)
    for parent in list(buffers):
        _flush(parent)
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


def filter_by_keys(areas: list[dict], keys: list[str]) -> list[dict]:
    """The areas at or under ANY key in `keys` in the key hierarchy. Pure.

    Empty `keys` keeps everything. Areas with an empty area_key
    (docs-derived, remainder) have no place in the hierarchy, so they only
    survive an empty keys list. Boundary via child_key_prefix_of — "backend"
    never captures "backend-jobs".
    """
    ks = [k for k in (str(k or "").strip() for k in keys) if k]
    if not ks:
        return list(areas)

    def _match(key: str) -> bool:
        return bool(key) and any(
            key == k or child_key_prefix_of(k, key) for k in ks
        )

    return [a for a in areas if _match(str(a.get("area_key") or ""))]


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
    a = area or {}
    # Bundles carry area_keys; feature areas a singular area_key;
    # docs-derived areas neither → [].
    keys = a.get("area_keys")
    if keys is None:
        key = str(a.get("area_key") or "")
        keys = [key] if key else []
    part = {
        "idx": idx,
        "kind": kind,
        "title": title,
        "scope_paths": list(a.get("scope_paths") or []),
        "doc_uids": list(a.get("doc_uids") or []),
        "lens_keys": list(lens_keys),
        "run_uid": "",
        "state": "pending",
        "file_count": a.get("file_count"),
        "area_keys": [str(k) for k in keys],
    }
    # Degraded areas (guessed partition — tree unavailable) mark their parts so
    # the campaign/run can surface that this part audits a guessed scope.
    if a.get("degraded"):
        part["degraded"] = True
    return part


def build_plan_by_kind(
    kind: str,
    areas: list[dict],
    lenses: list[dict],
    *,
    selection: str = "all",
    k: int = 3,
    path_recency: dict | None = None,
    feature_areas: list[dict] | None = None,
) -> list[dict]:
    """Kind-dispatched plan builder. Additive alongside the existing build_plan.

    kind="subsystem": one kind="area" part per area, lens_keys = all enabled
        lenses. selection filters: all=every area; stale=areas where
        area.get("stale"); rotation=k least-recently-covered via _area_recency.
    kind="feature": one kind="feature" part per leaf in feature_areas, lens_keys
        = all enabled lenses. selection: all=every leaf; stale/rotation=stale
        leaves only.
    kind="global": one kind="global" part per enabled lens (each expected to
        carry a global_agent_key).
    kind="batch": returns [] (handled by batch.py).
    """
    enabled = [lens for lens in lenses if lens.get("enabled", True)]
    enabled_keys = [str(lens["key"]) for lens in enabled]

    def _global_part_bk(lens: dict) -> dict:
        return _part(0, "global", f"Global sweep — {lens['key']}", None, [str(lens["key"])])

    parts: list[dict]

    if kind == "subsystem":
        if selection == "stale":
            candidate_areas = [a for a in areas if a.get("stale")]
        elif selection == "rotation":
            recency = path_recency or {}
            scored = [(i, _area_recency(a, recency)) for i, a in enumerate(areas)]
            _EPOCH = datetime.min
            scored.sort(key=lambda pair: (pair[1] is not None, pair[1] or _EPOCH, pair[0]))
            candidate_areas = [areas[i] for i, _ in scored[: max(k, 0)]]
        else:  # all
            candidate_areas = list(areas)
        parts = [_part(0, "area", a["title"], a, enabled_keys) for a in candidate_areas]

    elif kind == "feature":
        leaves = list(feature_areas or [])
        if selection in ("stale", "rotation"):
            leaves = [fa for fa in leaves if fa.get("stale")]
        parts = [_part(0, "feature", fa["title"], fa, enabled_keys) for fa in leaves]

    elif kind == "global":
        parts = [_global_part_bk(lens) for lens in enabled]

    else:  # batch (and any unknown kind)
        return []

    for idx, part in enumerate(parts):
        part["idx"] = idx
    return parts
