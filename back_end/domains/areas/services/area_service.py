"""Area + AreaEdit service — the Area map's lifecycle.

Humans edit Areas directly; agents go through propose_area_edit. Accepting
an edit applies its full replacement (creating the area for new-area
proposals). The entire lifecycle is AreaEdit.status — Areas have no state
machine.

Keys are path-like ("backend/delivery/convergence"); the hierarchy is
derived from key prefixes, never stored. A human edit or accepted AreaEdit
counts as a review: it stamps last_reviewed_at and clears stale_paths.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.areas.models import (
    AREA_KINDS,
    Area,
    AreaEdit,
    area_is_stale,
    child_key_prefix_of,
    is_leaf,
)
from domains.areas.schemas import AreaDTO, AreaEditDTO, AreaEditStatus, UpdateAreaRequest
from domains.docs.services.doc_freshness import watches_path
from infrastructure.audit import write_audit


def area_to_dto(a: Area, *, pending_edits: int = 0) -> AreaDTO:
    return AreaDTO(
        uid=a.uid,
        repository_uid=a.repository_uid,
        key=a.key,
        kind=a.kind or "subsystem",
        title=a.title or "",
        scope_paths=list(a.scope_paths or []),
        spec=a.spec or "",
        doc_uids=list(a.doc_uids or []),
        enabled=bool(a.enabled),
        provenance=a.provenance or "system",
        stale=area_is_stale(a),
        stale_paths=list(a.stale_paths or []),
        code_changed_at=a.code_changed_at,
        last_reviewed_at=a.last_reviewed_at,
        pending_edits=pending_edits,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def edit_to_dto(e: AreaEdit, *, current_spec: str = "") -> AreaEditDTO:
    return AreaEditDTO(
        uid=e.uid,
        repository_uid=e.repository_uid,
        area_uid=e.area_uid or "",
        key=e.key or "",
        kind=e.kind or "",
        title=e.title or "",
        scope_paths=list(e.scope_paths or []),
        doc_uids=list(e.doc_uids or []),
        proposed_spec=e.proposed_spec or "",
        rationale=e.rationale or "",
        source_run_uid=e.source_run_uid or "",
        status=AreaEditStatus(e.status or "pending"),
        resolved_by=e.resolved_by or "",
        resolved_at=e.resolved_at,
        created_at=e.created_at,
        current_spec=current_spec,
    )


def normalize_key(value: str) -> str:
    """Path-like keys: each "/"-segment is normalized independently, empty
    segments dropped ("Backend//Delivery Queue" → "backend/delivery-queue")."""
    segments = [
        re.sub(r"[^a-z0-9]+", "-", seg.strip().lower()).strip("-")
        for seg in (value or "").split("/")
    ]
    return "/".join(s for s in segments if s)[:120]


def _mark_reviewed(a: Area, now: datetime) -> None:
    a.last_reviewed_at = now
    a.stale_paths = []


async def list_areas(repository_uid: str) -> list[AreaDTO]:
    areas = [a for a in await Area.nodes.all() if a.repository_uid == repository_uid]
    pending = [
        e
        for e in await AreaEdit.nodes.all()
        if e.repository_uid == repository_uid and e.status == "pending"
    ]
    counts: dict[str, int] = {}
    for e in pending:
        if e.area_uid:
            counts[e.area_uid] = counts.get(e.area_uid, 0) + 1
    areas.sort(key=lambda a: a.key)
    return [area_to_dto(a, pending_edits=counts.get(a.uid, 0)) for a in areas]


async def count_pending_new_areas(repository_uid: str) -> int:
    """Pending new-area proposals (area_uid="") have no area to badge —
    surfaced as their own count."""
    return sum(
        1
        for e in await AreaEdit.nodes.all()
        if e.repository_uid == repository_uid
        and e.status == "pending"
        and not e.area_uid
    )


async def get_area(uid: str) -> Area:
    a = await Area.nodes.get_or_none(uid=uid)
    if a is None:
        raise HTTPException(status_code=404, detail=f"Area {uid} not found")
    return a


async def get_area_by_key(repository_uid: str, key: str) -> Area | None:
    for a in await Area.nodes.all():
        if a.repository_uid == repository_uid and a.key == key:
            return a
    return None


async def create_area(
    *,
    repository_uid: str,
    key: str,
    kind: str = "subsystem",
    title: str = "",
    scope_paths: list[str] | None = None,
    spec: str = "",
    doc_uids: list[str] | None = None,
    provenance: str = "system",
    actor: str = "human",
) -> Area:
    key = normalize_key(key)
    if not key:
        raise HTTPException(status_code=422, detail="key is required")
    if kind not in AREA_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {sorted(AREA_KINDS)}"
        )
    if await get_area_by_key(repository_uid, key) is not None:
        raise HTTPException(status_code=409, detail=f"Area key '{key}' already exists")
    a = Area(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        key=key,
        kind=kind,
        title=title or key.rsplit("/", 1)[-1].replace("-", " ").title(),
        scope_paths=list(scope_paths or []),
        spec=spec,
        doc_uids=list(doc_uids or []),
        provenance=provenance,
        last_reviewed_at=datetime.now(UTC),
    )
    await a.save()
    await write_audit(
        kind="area.created",
        subject_uid=a.uid,
        subject_type="Area",
        actor_uid=actor,
        payload={"key": key, "kind": kind, "repository_uid": repository_uid},
    )
    return a


async def update_area(uid: str, req: UpdateAreaRequest, *, actor: str = "human") -> Area:
    a = await get_area(uid)
    if req.kind is not None:
        if req.kind not in AREA_KINDS:
            raise HTTPException(
                status_code=422, detail=f"kind must be one of {sorted(AREA_KINDS)}"
            )
        a.kind = req.kind
    if req.title is not None:
        a.title = req.title
    if req.scope_paths is not None:
        a.scope_paths = list(req.scope_paths)
    if req.spec is not None:
        a.spec = req.spec
    if req.doc_uids is not None:
        a.doc_uids = list(req.doc_uids)
    if req.enabled is not None:
        a.enabled = req.enabled
    now = datetime.now(UTC)
    a.updated_at = now
    _mark_reviewed(a, now)  # a human edit counts as a review
    await a.save()
    await write_audit(
        kind="area.updated",
        subject_uid=a.uid,
        subject_type="Area",
        actor_uid=actor,
        payload={"key": a.key},
    )
    return a


async def delete_area(uid: str, *, actor: str = "human") -> None:
    a = await get_area(uid)
    # Pending edits against a deleted area are moot.
    for e in await AreaEdit.nodes.all():
        if e.area_uid == uid and e.status == "pending":
            e.status = "rejected"
            e.resolved_by = actor
            e.resolved_at = datetime.now(UTC)
            await e.save()
    await write_audit(
        kind="area.deleted",
        subject_uid=a.uid,
        subject_type="Area",
        actor_uid=actor,
        payload={"key": a.key, "repository_uid": a.repository_uid},
    )
    await a.delete()


# ---------- AreaEdits ----------


async def list_area_edits(
    repository_uid: str, *, status: str = "pending"
) -> list[AreaEditDTO]:
    edits = [
        e
        for e in await AreaEdit.nodes.all()
        if e.repository_uid == repository_uid and (not status or e.status == status)
    ]
    edits.sort(key=lambda e: e.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    out: list[AreaEditDTO] = []
    for e in edits:
        current_spec = ""
        if e.area_uid:
            a = await Area.nodes.get_or_none(uid=e.area_uid)
            current_spec = (a.spec or "") if a else ""
        out.append(edit_to_dto(e, current_spec=current_spec))
    return out


async def propose_area_edit(
    *,
    repository_uid: str,
    proposed_spec: str,
    rationale: str = "",
    key: str = "",
    kind: str = "subsystem",
    title: str = "",
    scope_paths: list[str] | None = None,
    doc_uids: list[str] | None = None,
    source_run_uid: str = "",
) -> dict:
    """Agent-facing: propose a full replacement for an existing area
    (matched by key) or a new area (unknown key). One pending edit per
    (area, run) — a second proposal from the same run replaces the first."""
    key = normalize_key(key)
    if not key:
        raise HTTPException(status_code=422, detail="key is required")
    if kind not in AREA_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {sorted(AREA_KINDS)}"
        )
    target = await get_area_by_key(repository_uid, key)
    area_uid = target.uid if target else ""

    if source_run_uid:
        for e in await AreaEdit.nodes.all():
            if (
                e.repository_uid == repository_uid
                and e.status == "pending"
                and e.source_run_uid == source_run_uid
                and (e.area_uid or "") == area_uid
                and (area_uid or e.key == key)
            ):
                await e.delete()

    e = AreaEdit(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        area_uid=area_uid,
        key=key,
        kind=kind,
        title=title,
        scope_paths=list(scope_paths or []),
        doc_uids=list(doc_uids or []),
        proposed_spec=proposed_spec,
        rationale=rationale,
        source_run_uid=source_run_uid,
        status="pending",
    )
    await e.save()
    await write_audit(
        kind="area_edit.proposed",
        subject_uid=e.uid,
        subject_type="AreaEdit",
        actor_uid=source_run_uid or "agent",
        payload={"key": key, "area_uid": area_uid, "new_area": not area_uid},
    )
    return {
        "status": "ok",
        "area_edit_uid": e.uid,
        "area_uid": area_uid,
        "new_area": not area_uid,
    }


async def get_area_edit(uid: str) -> AreaEdit:
    e = await AreaEdit.nodes.get_or_none(uid=uid)
    if e is None:
        raise HTTPException(status_code=404, detail=f"AreaEdit {uid} not found")
    return e


def _paths_overlap(a: str, b: str) -> bool:
    """Equal, or one is a "/"-boundary path-prefix of the other — the same
    boundary rule doc freshness uses for watch_paths."""
    return watches_path([a], b) or watches_path([b], a)


def validate_area_edit(edit, existing_areas: list[dict]) -> list[str]:
    """Warnings only, never raises — pure over (edit fields, area dicts).

    `existing_areas` rows carry {key, kind, scope_paths, enabled}. Checks the
    partition invariants a human should eyeball before accepting: leaf-vs-leaf
    scope overlap for subsystem/ignore areas (parent/child keys exempt — a
    parent legitimately spans its children) and ignore areas proposed without
    a reason. Feature areas are overlays: never warned about.
    """
    warnings: list[str] = []
    kind = getattr(edit, "kind", "") or "subsystem"
    key = getattr(edit, "key", "") or ""
    if kind == "ignore" and not (getattr(edit, "proposed_spec", "") or "").strip():
        warnings.append(
            "ignore area without a reason — the spec should say why these "
            "files are not auditable"
        )
    if kind == "feature":
        return warnings

    enabled = [a for a in existing_areas if a.get("enabled", True)]
    enabled_keys = [a["key"] for a in enabled if a.get("key")]
    if not is_leaf(key, enabled_keys):
        return warnings  # groupings don't own files; their children do

    for scope in getattr(edit, "scope_paths", None) or []:
        for other in enabled:
            other_key = other.get("key") or ""
            if not other_key or other_key == key:
                continue
            if other.get("kind", "subsystem") not in ("subsystem", "ignore"):
                continue
            if not is_leaf(other_key, enabled_keys):
                continue
            # Parent/child key relationships are exempt: nesting is hierarchy,
            # not a partition violation.
            if child_key_prefix_of(key, other_key) or child_key_prefix_of(other_key, key):
                continue
            for other_scope in other.get("scope_paths") or []:
                if _paths_overlap(scope, other_scope):
                    warnings.append(
                        f"scope '{scope}' overlaps leaf '{other_key}' ('{other_scope}')"
                    )
    return warnings


async def _repo_area_rows(repository_uid: str) -> list[dict]:
    return [
        {
            "key": a.key,
            "kind": a.kind or "subsystem",
            "scope_paths": list(a.scope_paths or []),
            "enabled": bool(a.enabled),
        }
        for a in await Area.nodes.all()
        if a.repository_uid == repository_uid
    ]


async def accept_area_edit(uid: str, *, actor: str = "human") -> tuple[Area, list[str]]:
    e = await get_area_edit(uid)
    if e.status != "pending":
        raise HTTPException(status_code=409, detail=f"AreaEdit is {e.status}, not pending")
    now = datetime.now(UTC)
    if e.area_uid:
        a = await Area.nodes.get_or_none(uid=e.area_uid)
        if a is None:
            raise HTTPException(status_code=409, detail="target Area no longer exists")
        # Full replacement: the edit carries the area's next shape.
        a.spec = e.proposed_spec or ""
        if e.key:
            a.key = e.key
        if e.kind:
            a.kind = e.kind
        if e.title:
            a.title = e.title
        if e.scope_paths:
            a.scope_paths = list(e.scope_paths)
        if e.doc_uids:
            a.doc_uids = list(e.doc_uids)
        a.updated_at = now
        _mark_reviewed(a, now)  # an accepted edit counts as a review
        await a.save()
    else:
        # create_area 409s when the key was claimed since the proposal.
        a = await create_area(
            repository_uid=e.repository_uid,
            key=e.key,
            kind=e.kind or "subsystem",
            title=e.title,
            scope_paths=list(e.scope_paths or []),
            spec=e.proposed_spec or "",
            doc_uids=list(e.doc_uids or []),
            provenance="agent",
            actor=actor,
        )
    warnings = validate_area_edit(
        e,
        [r for r in await _repo_area_rows(e.repository_uid) if r["key"] != a.key],
    )
    e.status = "accepted"
    e.area_uid = a.uid
    e.resolved_by = actor
    e.resolved_at = now
    await e.save()
    await write_audit(
        kind="area_edit.accepted",
        subject_uid=e.uid,
        subject_type="AreaEdit",
        actor_uid=actor,
        payload={"area_uid": a.uid, "key": a.key, "warnings": warnings},
    )
    return a, warnings


async def reject_area_edit(uid: str, *, actor: str = "human") -> AreaEditDTO:
    e = await get_area_edit(uid)
    if e.status != "pending":
        raise HTTPException(status_code=409, detail=f"AreaEdit is {e.status}, not pending")
    e.status = "rejected"
    e.resolved_by = actor
    e.resolved_at = datetime.now(UTC)
    await e.save()
    await write_audit(
        kind="area_edit.rejected",
        subject_uid=e.uid,
        subject_type="AreaEdit",
        actor_uid=actor,
        payload={"area_uid": e.area_uid, "key": e.key},
    )
    return edit_to_dto(e)
