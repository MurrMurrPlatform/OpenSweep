"""Campaign lifecycle: create (plan), launch, cancel, read.

Planning loads the repository, its docs, the live file tree, and the lens
catalog, then hands the pure planner the partition work. Tree resolution
degrades to watch-path-only areas on ANY failure — planning never fails
because GitHub was unreachable. Status only moves through the legality
matrix; every mutation appends a timeline event with the same
refetch-before-save discipline as thread_service.record_event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.campaigns.models import (
    CAMPAIGN_KINDS,
    CAMPAIGN_TEMPLATES,
    Campaign,
    is_legal_status_transition,
)
from domains.campaigns.schemas import CampaignDTO, CreateCampaignRequest
from domains.campaigns.services import planner
from domains.repositories.services.file_tree import file_tree_paths
from infrastructure.audit import write_audit
from logging_config import logger


def to_dto(c: Campaign) -> CampaignDTO:
    return CampaignDTO(
        uid=c.uid,
        repository_uid=c.repository_uid,
        title=c.title or "",
        status=c.status or "planning",
        template=c.template or "rotation",
        kind=str(getattr(c, "kind", "subsystem") or "subsystem"),
        selection=str(getattr(c, "selection", "all") or "all"),
        coverage_keys=list(getattr(c, "coverage_keys", []) or []),
        parent_uid=str(getattr(c, "parent_uid", "") or ""),
        child_uids=list(getattr(c, "child_uids", []) or []),
        effort=c.effort or "",
        lens_keys=list(c.lens_keys or []),
        k=int(getattr(c, "k", 3) or 3),
        area_prefix=str(getattr(c, "area_prefix", "") or ""),
        parts=[dict(p) for p in (c.parts or [])],
        max_parallel=int(c.max_parallel or 2),
        created_by=c.created_by or "",
        trigger_provenance=c.trigger_provenance or "",
        summary=dict(c.summary or {}),
        plan_summary=dict(getattr(c, "plan_summary", {}) or {}),
        events=list(c.events or []),
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


async def get(uid: str) -> Campaign:
    c = await Campaign.nodes.get_or_none(uid=uid)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Campaign {uid} not found")
    return c


async def list_for_repo(repository_uid: str) -> list[Campaign]:
    rows = await Campaign.nodes.filter(repository_uid=repository_uid)
    rows = list(rows)
    rows.sort(key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


async def record_event(campaign: Campaign, type: str, **payload) -> None:
    # ALWAYS reload before appending (thread_service.record_event): neomodel
    # save() writes EVERY declared property, so saving a stale node clobbers
    # fields written in between — parts/status updated by a concurrent tick.
    now = datetime.now(UTC)
    fresh = await Campaign.nodes.get_or_none(uid=campaign.uid) or campaign
    fresh.events = [
        *(fresh.events or []),
        {"ts": now.isoformat(), "type": type, **payload},
    ]
    fresh.updated_at = now
    await fresh.save()
    if fresh is not campaign:
        campaign.events = fresh.events
        campaign.status = fresh.status
        campaign.parts = fresh.parts
        campaign.updated_at = fresh.updated_at


async def _doc_inputs(repository_uid: str) -> list[dict]:
    """The repo's docs as the planner's input dicts."""
    from domains.docs.models import Doc

    return [
        {
            "uid": d.uid,
            "slug": d.slug or "",
            "title": d.title or "",
            "watch_paths": list(d.watch_paths or []),
        }
        for d in await Doc.nodes.all()
        if d.repository_uid == repository_uid
    ]


async def _area_map_inputs(repository_uid: str) -> dict | None:
    """The repo's enabled Area map as planner inputs, or None when the repo
    has NO enabled areas at all. subsystem_leaves may be empty (e.g. a
    features-only map) — _plan_areas then partitions from the docs but
    still carries the map's feature LEAVES through.

    Files belong to subsystem LEAVES only (parents are groupings); feature
    areas overlay at ANY depth, but the audit target is the feature LEAF —
    a feature is a leaf when no other enabled FEATURE key nests under it.
    Leaf-ness is computed over the subsystem key set and the feature key set
    SEPARATELY, so subsystem/feature hierarchies never mix. Parent feature
    groupings are groupings only (their spec is an optional charter, not an
    audit target) and are counted, not planned."""
    from domains.areas.models import Area, area_is_stale, is_leaf

    rows = [
        a
        for a in await Area.nodes.all()
        if a.repository_uid == repository_uid and bool(a.enabled)
    ]
    if not rows:
        return None
    subsystem_keys = [a.key for a in rows if (a.kind or "subsystem") == "subsystem"]
    feature_keys = [a.key for a in rows if a.kind == "feature"]

    def _leaf_dict(a) -> dict:
        return {
            "area_key": a.key,
            "title": a.title or a.key,
            "scope_paths": list(a.scope_paths or []),
            "doc_uids": list(a.doc_uids or []),
            # Unified staleness axis (code_changed_at > last_reviewed_at) +
            # whether the leaf has a spec — the rotation/focused feature-part
            # selector and generate-specs targeting both read these.
            "stale": area_is_stale(a),
            "has_spec": bool((a.spec or "").strip()),
        }

    subsystems = [a for a in rows if (a.kind or "subsystem") == "subsystem"]
    features = [a for a in rows if a.kind == "feature"]
    return {
        "subsystem_leaves": [
            _leaf_dict(a) for a in subsystems if is_leaf(a.key, subsystem_keys)
        ],
        # Feature LEAVES only — the audit targets. Parent feature groupings
        # are excluded (their spec is a charter, not a contract to verify).
        "feature_leaves": [
            _leaf_dict(a) for a in features if is_leaf(a.key, feature_keys)
        ],
        "ignore_scopes": sorted(
            {p for a in rows if a.kind == "ignore" for p in (a.scope_paths or [])}
        ),
        # Whole-map counts for the plan explanation: total enabled areas,
        # subsystem non-leaves (groupings, not audit targets), feature
        # non-leaves (groupings), ignore areas.
        "counts": {
            "map_areas": len(rows),
            "groupings": sum(1 for a in subsystems if not is_leaf(a.key, subsystem_keys)),
            "feature_groupings": sum(
                1 for a in features if not is_leaf(a.key, feature_keys)
            ),
            "ignored": sum(1 for a in rows if a.kind == "ignore"),
        },
    }


def _map_stats(map_inputs: dict | None) -> dict:
    """Whole-map counts for the plan explanation — zeros when the repo has
    no enabled areas at all (pure docs planning). `features` counts feature
    LEAVES (audit targets); `feature_groupings` the parent feature groupings."""
    if map_inputs is None:
        return {
            "map_areas": 0,
            "leaves": 0,
            "groupings": 0,
            "features": 0,
            "feature_groupings": 0,
            "ignored": 0,
        }
    counts = map_inputs["counts"]
    return {
        "map_areas": counts["map_areas"],
        "leaves": len(map_inputs["subsystem_leaves"]),
        "groupings": counts["groupings"],
        "features": len(map_inputs["feature_leaves"]),
        "feature_groupings": counts["feature_groupings"],
        "ignored": counts["ignored"],
    }


async def _plan_areas(
    repository_uid: str, repo
) -> tuple[list[dict], str, int, str, list[dict], dict, dict]:
    """(areas, degraded_reason — "" = full tree, total file count, source
    "area-map"|"docs", feature areas, partition health, map stats).

    The tree+partition half of planning — shared by the plan builder and
    the no-persist preview endpoint. A repo whose enabled Area map has
    subsystem leaves plans from them (source "area-map"); otherwise the
    docs' watch scopes (source "docs"). A map WITHOUT subsystem leaves
    (features-only) still contributes its feature areas on top of the
    docs partition, with the flip explained in degraded_reason. Health
    ({overlapping_files, dead_ignore_scopes}) only exists for area-map
    partitions — the docs partition is non-overlapping by construction.
    Map stats (_map_stats) count the whole enabled map regardless of which
    source ends up planning."""
    from domains.repositories.services.path_matching import watches_path

    def _count_features(features: list[dict], file_paths: list[str]) -> list[dict]:
        out = []
        for f in features:
            fa = dict(f)
            fa["file_count"] = (
                sum(
                    1
                    for p in file_paths
                    if watches_path(list(f.get("scope_paths") or []), p)
                )
                if file_paths
                else None
            )
            out.append(fa)
        return out

    file_paths, degraded_reason = await file_tree_paths(repo)
    map_inputs = await _area_map_inputs(repository_uid)
    if map_inputs is not None and map_inputs["subsystem_leaves"]:
        areas, health = planner.areas_from_map(
            map_inputs["subsystem_leaves"], map_inputs["ignore_scopes"], file_paths
        )
        feature_areas = _count_features(map_inputs["feature_leaves"], file_paths)
        return (
            areas,
            degraded_reason,
            len(file_paths),
            "area-map",
            feature_areas,
            health,
            _map_stats(map_inputs),
        )

    docs = await _doc_inputs(repository_uid)
    areas = planner.normalize_areas(docs, file_paths)
    feature_areas: list[dict] = []
    if map_inputs is not None:
        # Areas exist but none are enabled subsystem leaves: partition from
        # the docs, keep the map's feature leaves (full-template campaigns
        # keep their spec-audit parts), and say why the source flipped.
        feature_areas = _count_features(map_inputs["feature_leaves"], file_paths)
        note = "area map present but has no enabled subsystem leaves — planned from docs"
        degraded_reason = f"{degraded_reason}; {note}" if degraded_reason else note
    return (
        areas,
        degraded_reason,
        len(file_paths),
        "docs",
        feature_areas,
        {"overlapping_files": 0, "dead_ignore_scopes": []},
        _map_stats(map_inputs),
    )


async def _enabled_lenses(kind: str, lens_keys: list[str]) -> list[dict]:
    """The lens dicts a plan of `kind` runs against.

    Built from the enabled lens catalog as {key, global_agent_key, enabled} —
    NO scope (dropped from planning; a global lens is one carrying a
    global_agent_key). Empty `lens_keys` falls back to
    lens_service.default_lens_keys(kind); a non-empty selection is the
    intersection with the enabled set. A `global` kind keeps only lenses
    that name a global agent (their sweep is the whole point)."""
    from domains.lenses.services import lens_service

    catalog = [
        {
            "key": lens.key,
            "global_agent_key": lens.global_agent_key or "",
            "enabled": True,
        }
        for lens in await lens_service.list_lenses(enabled_only=True)
    ]
    keys = list(lens_keys) or lens_service.default_lens_keys(kind)
    wanted = set(keys)
    lenses = [lens for lens in catalog if lens["key"] in wanted]
    if kind == "global":
        lenses = [lens for lens in lenses if lens["global_agent_key"]]
    return lenses


async def _plan_parts(
    repository_uid: str,
    *,
    kind: str,
    coverage_keys: list[str],
    selection: str,
    lens_keys: list[str],
    k: int,
) -> tuple[list[dict], str, str, dict]:
    """(part list, degraded_reason, source, plan_summary) for a kind-aware
    plan.

    The ONE plan builder — create() and launch()'s replan both go through
    here so the two can never drift. Branches on `kind`:
      - subsystem: tree partition → filter_by_keys → bundle_siblings →
        build_plan_by_kind (rotation fetches path_recency).
      - feature: feature areas → filter_by_keys → build_plan_by_kind.
      - global: one part per global lens (no partition needed).
    plan_summary is the plan's own explanation; its shape is stable across
    sources so the UI can always narrate it (adds total_runs + by_kind)."""
    from domains.repositories.models import Repository
    from domains.runs.services.audit_selection import coverage_recency_for

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repository {repository_uid} not found")

    lenses = await _enabled_lenses(kind, lens_keys)

    if kind == "global":
        # A global sweep needs no partition — one part per global lens.
        parts = planner.build_plan_by_kind("global", [], lenses)
        source = "global"
        degraded_reason = ""
        map_stats = _map_stats(None)
        areas: list[dict] = []
    else:
        (
            areas,
            degraded_reason,
            _total,
            source,
            feature_areas,
            _health,
            map_stats,
        ) = await _plan_areas(repository_uid, repo)
        if kind == "feature":
            feature_areas = planner.filter_by_keys(feature_areas, coverage_keys)
            if coverage_keys and not feature_areas:
                note = f"coverage_keys {coverage_keys!r} matched no feature areas"
                degraded_reason = (
                    f"{degraded_reason}; {note}" if degraded_reason else note
                )
            parts = planner.build_plan_by_kind(
                "feature", [], lenses, selection=selection, feature_areas=feature_areas
            )
            areas = feature_areas
        else:  # subsystem
            areas = planner.filter_by_keys(areas, coverage_keys)
            if coverage_keys and not areas:
                note = f"coverage_keys {coverage_keys!r} matched no areas"
                degraded_reason = (
                    f"{degraded_reason}; {note}" if degraded_reason else note
                )
            if source == "area-map":
                # Undersized sibling leaves share one run — the map stays
                # fine-grained while parts stay worth a dispatch. Rotation
                # ranks the bundles (a bundle's recency is its union's
                # stalest path).
                areas = planner.bundle_siblings(areas)
            path_recency = None
            if selection == "rotation":
                path_recency = await coverage_recency_for(repository_uid)
            parts = planner.build_plan_by_kind(
                "subsystem",
                areas,
                lenses,
                selection=selection,
                k=k,
                path_recency=path_recency,
            )

    # The plan's own explanation — how the map's rows became this part list.
    # Map-level counts stay whole-map; part counts describe the actual plan.
    # bundled_leaves = leaves sharing a part with siblings (multi-key parts).
    area_parts = [p for p in parts if p["kind"] == "area"]
    by_kind = {
        "area": sum(1 for p in parts if p["kind"] == "area"),
        "feature": sum(1 for p in parts if p["kind"] == "feature"),
        "global": sum(1 for p in parts if p["kind"] == "global"),
    }
    plan_summary = {
        "source": source,
        **map_stats,
        "area_parts": len(area_parts),
        "bundled_leaves": sum(
            len(p["area_keys"]) for p in area_parts if len(p["area_keys"]) > 1
        ),
        "feature_parts": by_kind["feature"],
        "global_parts": by_kind["global"],
        "total_runs": len(parts),
        "by_kind": by_kind,
        "oversized": [str(a.get("title") or "") for a in areas if a.get("oversized")],
        "degraded": degraded_reason,
        "coverage_keys": list(coverage_keys),
        "selection": selection,
    }
    return parts, degraded_reason, source, plan_summary


async def preview_areas(repository_uid: str, *, area_prefix: str = "") -> dict:
    """The partition a campaign WOULD use, without persisting anything —
    backs GET /repositories/{uid}/campaign-areas. An `area_prefix` slices
    the listing to the areas at or under that key, exactly as a campaign
    planned with it would (health + totals stay whole-map)."""
    from domains.repositories.models import Repository

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repository {repository_uid} not found")
    areas, degraded_reason, total_files, source, feature_areas, health, _stats = (
        await _plan_areas(repository_uid, repo)
    )
    if area_prefix:
        areas = planner.filter_by_prefix(areas, area_prefix)
        feature_areas = planner.filter_by_prefix(feature_areas, area_prefix)
    uncovered = sum(
        int(a.get("file_count") or 0)
        for a in areas
        if str(a.get("title") or "").startswith(planner.REMAINDER_TITLE)
    )
    listed = [
        {
            "area_key": "",
            "kind": "subsystem",
            "oversized": False,
            "dead_scope_paths": [],
            **a,
        }
        for a in areas
    ] + [
        {"oversized": False, "dead_scope_paths": [], **fa, "kind": "feature"}
        for fa in feature_areas
    ]
    return {
        "areas": listed,
        "degraded": degraded_reason,
        "total_files": total_files,
        "uncovered_files": uncovered,
        "source": source,
        "oversized_areas": [
            str(a.get("title") or "") for a in listed if a.get("oversized")
        ],
        "overlapping_files": int(health.get("overlapping_files") or 0),
        "dead_ignore_scopes": list(health.get("dead_ignore_scopes") or []),
    }


def _resolve_kind(req: CreateCampaignRequest) -> tuple[str, list[str], str, list[str]]:
    """(kind, coverage_keys, selection, lens_keys) from the request.

    A non-empty `req.kind` is the new path — used verbatim. An empty kind is
    the legacy `template` path, translated: rotation→subsystem/rotation,
    focused→subsystem with only the first lens, full→batch, anything
    else→subsystem. `coverage_keys` defaults to [req.area_prefix] when unset
    but a prefix is given (legacy scoping)."""
    lens_keys = list(req.lens_keys or [])
    coverage_keys = list(req.coverage_keys or [])
    if not coverage_keys and (req.area_prefix or "").strip():
        coverage_keys = [(req.area_prefix or "").strip()]

    kind = (req.kind or "").strip()
    if kind:
        if kind not in CAMPAIGN_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"invalid kind {req.kind!r}; valid: {sorted(CAMPAIGN_KINDS)}",
            )
        selection = (req.selection or "all").strip() or "all"
        return kind, coverage_keys, selection, lens_keys

    # Legacy template path.
    template = (req.template or "rotation").strip()
    if template not in CAMPAIGN_TEMPLATES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid template {req.template!r}; valid: {sorted(CAMPAIGN_TEMPLATES)}",
        )
    if template == "rotation":
        return "subsystem", coverage_keys, "rotation", lens_keys
    if template == "focused":
        if not lens_keys:
            raise HTTPException(
                status_code=422,
                detail="focused campaigns need lens_keys[0] as the focus lens",
            )
        return "subsystem", coverage_keys, "all", [lens_keys[0]]
    if template == "full":
        return "batch", coverage_keys, "all", lens_keys
    return "subsystem", coverage_keys, "all", lens_keys


async def create(
    repository_uid: str,
    req: CreateCampaignRequest,
    *,
    created_by: str = "",
    trigger_provenance: str = "manual",
) -> Campaign:
    """Plan a campaign (status=planning — launch is the separate go signal)."""
    kind, coverage_keys, selection, lens_keys = _resolve_kind(req)

    if kind == "batch":
        # A batch fans out into three child campaigns — batch.py owns the
        # parent+children creation (lazy import to avoid a circular import).
        from domains.campaigns.services import batch

        return await batch.create_batch(
            repository_uid,
            req,
            created_by=created_by,
            trigger_provenance=trigger_provenance or "manual",
        )

    # Clamp k up front so the stored value and the plan (and any launch-time
    # replan) are built from the same number.
    k = max(int(req.k or 3), 1)
    parts, degraded_reason, source, plan_summary = await _plan_parts(
        repository_uid,
        kind=kind,
        coverage_keys=coverage_keys,
        selection=selection,
        lens_keys=lens_keys,
        k=k,
    )

    c = Campaign(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        title=req.title or f"{kind.capitalize()} audit campaign",
        status="planning",
        template=(req.template or "rotation").strip(),
        kind=kind,
        selection=selection,
        coverage_keys=coverage_keys,
        effort=(req.effort or "").strip(),
        lens_keys=lens_keys,
        k=k,
        area_prefix=(req.area_prefix or "").strip(),
        parts=parts,
        plan_summary=plan_summary,
        max_parallel=max(int(req.max_parallel or 2), 1),
        created_by=created_by,
        trigger_provenance=trigger_provenance or "manual",
    )
    await c.save()
    # A degraded plan (no/partial file tree) must be distinguishable from a
    # real one — users approve the plan's scope, so mark it on the record.
    planned_payload = {"parts": len(parts), "source": source}
    if degraded_reason:
        planned_payload["degraded"] = degraded_reason
    await record_event(c, "planned", **planned_payload)
    await write_audit(
        kind="campaign.planned",
        subject_uid=c.uid,
        subject_type="Campaign",
        actor_uid=created_by,
        repository_uid=repository_uid,
        payload={"kind": kind, "parts": len(parts), "source": source, **({"degraded": degraded_reason} if degraded_reason else {})},
    )
    return c


async def _transition(c: Campaign, to_status: str) -> None:
    if not is_legal_status_transition(c.status or "planning", to_status):
        raise HTTPException(
            status_code=409, detail=f"illegal transition {c.status} → {to_status}"
        )
    c.status = to_status
    await c.save()


async def _replan(c: Campaign) -> None:
    """Recompute the plan with the campaign's stored inputs and replace the
    parts when the world moved since planning (new doc pages, tree changes).

    A degraded recompute (tree unavailable/truncated) or any error keeps the
    existing parts — a stale-but-real plan beats a fresh-but-blind one."""
    try:
        parts, degraded_reason, _source, plan_summary = await _plan_parts(
            c.repository_uid,
            kind=str(getattr(c, "kind", "subsystem") or "subsystem"),
            coverage_keys=list(getattr(c, "coverage_keys", []) or []),
            selection=str(getattr(c, "selection", "all") or "all"),
            lens_keys=list(c.lens_keys or []),
            k=int(getattr(c, "k", 3) or 3),
        )
    except Exception as exc:  # noqa: BLE001 — launch must not fail on replan
        logger.warning(
            f"campaign {c.uid}: replan failed ({type(exc).__name__}: {exc}) — "
            "launching with the original parts",
            extra={"tag": "campaigns"},
        )
        await record_event(c, "replan_skipped", reason=f"{type(exc).__name__}: {exc}")
        return
    if degraded_reason:
        logger.warning(
            f"campaign {c.uid}: replan degraded ({degraded_reason}) — "
            "launching with the original parts",
            extra={"tag": "campaigns"},
        )
        await record_event(c, "replan_skipped", reason=degraded_reason)
        return
    if parts == list(c.parts or []):
        return
    was = len(c.parts or [])
    c.parts = parts
    c.plan_summary = plan_summary
    await c.save()
    await record_event(c, "replanned", parts=len(parts), was=was)


async def launch(uid: str, *, actor_uid: str = "") -> Campaign:
    """planning → running; the celery tick starts dispatching parts.

    The stored plan is a snapshot from creation time — replan first so the
    campaign runs against today's docs and tree, not a stale partition.

    A batch parent owns no parts of its own — it launches its children and
    moves to running via batch.launch_batch instead of the normal replan."""
    c = await get(uid)
    if str(getattr(c, "kind", "subsystem") or "subsystem") == "batch":
        from domains.campaigns.services import batch

        await batch.launch_batch(c)
        return c
    if (c.status or "planning") == "planning":
        await _replan(c)
    await _transition(c, "running")
    await record_event(c, "launched", by=actor_uid)
    return c


async def preview_plan(repository_uid: str, req: CreateCampaignRequest) -> dict:
    """The plan a campaign WOULD produce, computed live, never persisted —
    backs the new-campaign dialog's run-count preview. Runs the SAME
    kind-aware planning path as create() (minus batch, which has no single
    plan) and reports the shape a launch would dispatch."""
    kind, coverage_keys, selection, lens_keys = _resolve_kind(req)
    if kind == "batch":
        # A batch has no single plan — preview each child kind and total.
        from domains.lenses.services import lens_service

        by_kind: dict[str, int] = {"area": 0, "feature": 0, "global": 0}
        areas_all: list[dict] = []
        uncovered = 0
        oversized: list[str] = []
        degraded = ""
        for child_kind in ("subsystem", "feature", "global"):
            parts, child_degraded, _source, summary = await _plan_parts(
                repository_uid,
                kind=child_kind,
                coverage_keys=coverage_keys,
                selection=selection,
                lens_keys=list(lens_service.default_lens_keys(child_kind)),
                k=max(int(req.k or 3), 1),
            )
            for key, val in summary["by_kind"].items():
                by_kind[key] += val
            oversized.extend(summary.get("oversized") or [])
            if child_degraded and not degraded:
                degraded = child_degraded
        return {
            "total_runs": sum(by_kind.values()),
            "by_kind": by_kind,
            "areas": areas_all,
            "uncovered_files": uncovered,
            "oversized": oversized,
            "degraded": degraded,
            "source": "batch",
        }

    k = max(int(req.k or 3), 1)
    parts, degraded_reason, source, plan_summary = await _plan_parts(
        repository_uid,
        kind=kind,
        coverage_keys=coverage_keys,
        selection=selection,
        lens_keys=lens_keys,
        k=k,
    )
    areas = [
        {
            "title": p.get("title") or "",
            "kind": p.get("kind") or "area",
            "scope_paths": list(p.get("scope_paths") or []),
            "area_keys": list(p.get("area_keys") or []),
            "file_count": p.get("file_count"),
        }
        for p in parts
    ]
    return {
        "total_runs": plan_summary["total_runs"],
        "by_kind": plan_summary["by_kind"],
        "areas": areas,
        "uncovered_files": 0,
        "oversized": list(plan_summary.get("oversized") or []),
        "degraded": degraded_reason,
        "source": source,
    }


async def cancel(uid: str, *, reason: str = "", actor_uid: str = "") -> Campaign:
    """planning/running → cancelled. In-flight child runs are left to
    finish on their own; parts stay as-is for the record."""
    c = await get(uid)
    await _transition(c, "cancelled")
    await record_event(c, "cancelled", reason=reason, by=actor_uid)
    return c


async def delete(uid: str, *, actor_uid: str = "") -> None:
    """Remove the campaign record entirely. Live campaigns (running/
    finalizing) must be cancelled first — deleting mid-flight would pull the
    plan out from under the celery tick. Child runs are kept: they are the
    audit record, and the digest already landed as findings."""
    c = await get(uid)
    status = c.status or "planning"
    if status in {"running", "finalizing"}:
        raise HTTPException(
            status_code=409,
            detail=f"campaign is {status} — cancel it before deleting",
        )
    repository_uid = c.repository_uid
    title = c.title
    await c.delete()
    await write_audit(
        kind="campaign.deleted",
        subject_uid=uid,
        subject_type="Campaign",
        actor_uid=actor_uid,
        repository_uid=repository_uid,
        payload={"status": status, "title": title},
    )
