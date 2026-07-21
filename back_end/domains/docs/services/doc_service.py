"""Doc + DocEdit service (KNOWLEDGE_V3_DOCUMENTATION.md).

Humans edit Docs directly; agents go through propose_doc_edit. Accepting an
edit applies its full replacement body (creating the page for new-page
proposals). The entire lifecycle is DocEdit.status — Docs have no state
machine.

Slugs are path-like ("backend/queue-workers"); folders are derived from
slug prefixes, never stored. A human edit or accepted DocEdit counts as a
review: it stamps last_reviewed_at and clears stale_paths (§9).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.docs.models import CONVENTIONS_SLUG, Doc, DocEdit, doc_is_stale
from domains.docs.schemas import DocDTO, DocEditDTO, DocEditStatus
from infrastructure.audit import write_audit


def doc_to_dto(d: Doc, *, pending_edits: int = 0) -> DocDTO:
    return DocDTO(
        uid=d.uid,
        repository_uid=d.repository_uid,
        slug=d.slug,
        title=d.title or "",
        summary=d.summary or "",
        body=d.body or "",
        pinned=bool(d.pinned),
        watch_paths=list(d.watch_paths or []),
        stale=doc_is_stale(d),
        stale_paths=list(d.stale_paths or []),
        code_changed_at=d.code_changed_at,
        last_reviewed_at=d.last_reviewed_at,
        pending_edits=pending_edits,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def edit_to_dto(e: DocEdit, *, current_body: str = "") -> DocEditDTO:
    return DocEditDTO(
        uid=e.uid,
        repository_uid=e.repository_uid,
        doc_uid=e.doc_uid or "",
        slug=e.slug or "",
        title=e.title or "",
        summary=e.summary or "",
        watch_paths=list(e.watch_paths or []),
        proposed_body=e.proposed_body or "",
        rationale=e.rationale or "",
        source_run_uid=e.source_run_uid or "",
        status=DocEditStatus(e.status or "pending"),
        resolved_by=e.resolved_by or "",
        resolved_at=e.resolved_at,
        created_at=e.created_at,
        current_body=current_body,
    )


def normalize_slug(value: str) -> str:
    """Path-like slugs: each "/"-segment is normalized independently, empty
    segments dropped ("Backend//Queue Workers" → "backend/queue-workers")."""
    segments = [
        re.sub(r"[^a-z0-9]+", "-", seg.strip().lower()).strip("-")
        for seg in (value or "").split("/")
    ]
    return "/".join(s for s in segments if s)[:120]


def _mark_reviewed(d: Doc, now: datetime) -> None:
    d.last_reviewed_at = now
    d.stale_paths = []


async def list_docs(repository_uid: str) -> list[DocDTO]:
    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
    pending = [
        e
        for e in await DocEdit.nodes.all()
        if e.repository_uid == repository_uid and e.status == "pending"
    ]
    counts: dict[str, int] = {}
    for e in pending:
        if e.doc_uid:
            counts[e.doc_uid] = counts.get(e.doc_uid, 0) + 1
    docs.sort(key=lambda d: (not bool(d.pinned), d.slug))
    return [doc_to_dto(d, pending_edits=counts.get(d.uid, 0)) for d in docs]


async def count_pending_new_pages(repository_uid: str) -> int:
    """Pending new-page proposals (doc_uid="") have no page to badge —
    surfaced as their own count."""
    return sum(
        1
        for e in await DocEdit.nodes.all()
        if e.repository_uid == repository_uid
        and e.status == "pending"
        and not e.doc_uid
    )


async def get_doc(uid: str) -> Doc:
    d = await Doc.nodes.get_or_none(uid=uid)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Doc {uid} not found")
    return d


async def get_doc_by_slug(repository_uid: str, slug: str) -> Doc | None:
    for d in await Doc.nodes.all():
        if d.repository_uid == repository_uid and d.slug == slug:
            return d
    return None


async def create_doc(
    *,
    repository_uid: str,
    slug: str,
    title: str = "",
    summary: str = "",
    body: str = "",
    watch_paths: list[str] | None = None,
    pinned: bool = False,
    actor: str = "human",
) -> Doc:
    slug = normalize_slug(slug)
    if not slug:
        raise HTTPException(status_code=422, detail="slug is required")
    if await get_doc_by_slug(repository_uid, slug) is not None:
        raise HTTPException(status_code=409, detail=f"Doc slug '{slug}' already exists")
    d = Doc(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        slug=slug,
        title=title or slug.rsplit("/", 1)[-1].replace("-", " ").title(),
        summary=summary,
        body=body,
        watch_paths=list(watch_paths or []),
        pinned=pinned,
        last_reviewed_at=datetime.now(UTC),
    )
    await d.save()
    await write_audit(
        kind="doc.created",
        subject_uid=d.uid,
        subject_type="Doc",
        actor_uid=actor,
        payload={"slug": slug, "repository_uid": repository_uid},
    )
    return d


async def update_doc(
    uid: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    body: str | None = None,
    watch_paths: list[str] | None = None,
    actor: str = "human",
) -> Doc:
    d = await get_doc(uid)
    if title is not None:
        d.title = title
    if summary is not None:
        d.summary = summary
    if body is not None:
        d.body = body
    if watch_paths is not None:
        d.watch_paths = list(watch_paths)
    now = datetime.now(UTC)
    d.updated_at = now
    _mark_reviewed(d, now)  # a human edit counts as a review
    await d.save()
    await write_audit(
        kind="doc.updated",
        subject_uid=d.uid,
        subject_type="Doc",
        actor_uid=actor,
        payload={"slug": d.slug},
    )
    return d


async def delete_doc(uid: str, *, actor: str = "human") -> None:
    d = await get_doc(uid)
    # Pending edits against a deleted page are moot.
    for e in await DocEdit.nodes.all():
        if e.doc_uid == uid and e.status == "pending":
            e.status = "rejected"
            e.resolved_by = actor
            e.resolved_at = datetime.now(UTC)
            await e.save()
    # Detach memories anchored to this page: keep their content but clear the
    # dangling freshness anchor so it can't point at a deleted Doc.
    from domains.memory.models import Memory

    for m in await Memory.nodes.filter(anchor_uid=uid):
        m.anchor_uid = ""
        await m.save()
    await write_audit(
        kind="doc.deleted",
        subject_uid=d.uid,
        subject_type="Doc",
        actor_uid=actor,
        payload={"slug": d.slug, "repository_uid": d.repository_uid},
    )
    await d.delete()


async def reset_docs(repository_uid: str, *, actor: str = "human") -> dict:
    """Destructive: delete EVERY Doc and DocEdit for the repository.

    A clean regenerate beats hand-pruning a wrong tree. Memories anchored
    to deleted pages keep their content but lose the freshness anchor
    (same as single-page delete); Checked stamps stay as history. One
    audit event with counts; irreversible."""
    from domains.memory.models import Memory

    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
    edits = [e for e in await DocEdit.nodes.all() if e.repository_uid == repository_uid]
    doc_uids = {d.uid for d in docs}
    for m in await Memory.nodes.all():
        if (m.anchor_uid or "") in doc_uids:
            m.anchor_uid = ""
            await m.save()
    for e in edits:
        await e.delete()
    for d in docs:
        await d.delete()
    await write_audit(
        kind="docs.reset",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=actor,
        repository_uid=repository_uid,
        payload={"docs_deleted": len(docs), "edits_deleted": len(edits)},
    )
    return {"docs_deleted": len(docs), "edits_deleted": len(edits)}


async def set_pinned(uid: str, *, pinned: bool, actor: str = "human") -> Doc:
    d = await get_doc(uid)
    d.pinned = pinned
    d.updated_at = datetime.now(UTC)
    await d.save()
    return d


async def seed_conventions_doc(repository_uid: str) -> Doc | None:
    """Every repository gets one pinned, empty conventions page on
    registration so agents always have a target for propose_doc_edit."""
    if await get_doc_by_slug(repository_uid, CONVENTIONS_SLUG) is not None:
        return None
    return await create_doc(
        repository_uid=repository_uid,
        slug=CONVENTIONS_SLUG,
        title="Conventions",
        summary="How code is written in this repository.",
        body="",
        pinned=True,
        actor="system",
    )


# ---------- DocEdits ----------


async def list_doc_edits(
    repository_uid: str, *, status: str = "pending"
) -> list[DocEditDTO]:
    edits = [
        e
        for e in await DocEdit.nodes.all()
        if e.repository_uid == repository_uid and (not status or e.status == status)
    ]
    edits.sort(key=lambda e: e.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    out: list[DocEditDTO] = []
    for e in edits:
        current_body = ""
        if e.doc_uid:
            d = await Doc.nodes.get_or_none(uid=e.doc_uid)
            current_body = (d.body or "") if d else ""
        out.append(edit_to_dto(e, current_body=current_body))
    return out


async def propose_doc_edit(
    *,
    repository_uid: str,
    proposed_body: str,
    rationale: str = "",
    slug: str = "",
    title: str = "",
    summary: str = "",
    watch_paths: list[str] | None = None,
    source_run_uid: str = "",
) -> DocEdit:
    """Agent-facing: propose a full replacement body for an existing page
    (matched by slug) or a new page (unknown slug). One pending edit per
    (doc, run) — a second proposal from the same run replaces the first."""
    slug = normalize_slug(slug)
    target = await get_doc_by_slug(repository_uid, slug) if slug else None
    doc_uid = target.uid if target else ""

    if source_run_uid:
        for e in await DocEdit.nodes.all():
            if (
                e.repository_uid == repository_uid
                and e.status == "pending"
                and e.source_run_uid == source_run_uid
                and (e.doc_uid or "") == doc_uid
                and (doc_uid or e.slug == slug)
            ):
                await e.delete()

    e = DocEdit(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        doc_uid=doc_uid,
        slug=slug,
        title=title,
        summary=summary,
        watch_paths=list(watch_paths or []),
        proposed_body=proposed_body,
        rationale=rationale,
        source_run_uid=source_run_uid,
        status="pending",
    )
    await e.save()
    await write_audit(
        kind="doc_edit.proposed",
        subject_uid=e.uid,
        subject_type="DocEdit",
        actor_uid=source_run_uid or "agent",
        payload={"slug": slug, "doc_uid": doc_uid, "new_page": not doc_uid},
    )
    return e


async def get_doc_edit(uid: str) -> DocEdit:
    e = await DocEdit.nodes.get_or_none(uid=uid)
    if e is None:
        raise HTTPException(status_code=404, detail=f"DocEdit {uid} not found")
    return e


async def accept_doc_edit(uid: str, *, actor: str = "human") -> DocDTO:
    e = await get_doc_edit(uid)
    if e.status != "pending":
        raise HTTPException(status_code=409, detail=f"DocEdit is {e.status}, not pending")
    now = datetime.now(UTC)
    if e.doc_uid:
        d = await Doc.nodes.get_or_none(uid=e.doc_uid)
        if d is None:
            raise HTTPException(status_code=409, detail="target Doc no longer exists")
        d.body = e.proposed_body or ""
        if e.title:
            d.title = e.title
        if e.summary:
            d.summary = e.summary
        if e.watch_paths:
            d.watch_paths = list(e.watch_paths)
        d.updated_at = now
        _mark_reviewed(d, now)  # an accepted edit counts as a review
        await d.save()
    else:
        d = await create_doc(
            repository_uid=e.repository_uid,
            slug=e.slug or f"page-{e.uid[:8]}",
            title=e.title,
            summary=e.summary,
            body=e.proposed_body or "",
            watch_paths=list(e.watch_paths or []),
            actor=actor,
        )
    e.status = "accepted"
    e.doc_uid = d.uid
    e.resolved_by = actor
    e.resolved_at = now
    await e.save()
    await write_audit(
        kind="doc_edit.accepted",
        subject_uid=e.uid,
        subject_type="DocEdit",
        actor_uid=actor,
        payload={"doc_uid": d.uid, "slug": d.slug},
    )
    return doc_to_dto(d)


async def reject_doc_edit(uid: str, *, actor: str = "human") -> DocEditDTO:
    e = await get_doc_edit(uid)
    if e.status != "pending":
        raise HTTPException(status_code=409, detail=f"DocEdit is {e.status}, not pending")
    e.status = "rejected"
    e.resolved_by = actor
    e.resolved_at = datetime.now(UTC)
    await e.save()
    await write_audit(
        kind="doc_edit.rejected",
        subject_uid=e.uid,
        subject_type="DocEdit",
        actor_uid=actor,
        payload={"doc_uid": e.doc_uid, "slug": e.slug},
    )
    return edit_to_dto(e)
