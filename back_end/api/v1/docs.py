"""Documentation API — Doc pages + DocEdit review (KNOWLEDGE_V3).

Humans edit pages directly (no workflow); agent proposals arrive as
DocEdits and are accepted/rejected here — individually or in bulk (a
generated page tree is one decision, not fifteen). Draft/verify dispatch
LLM runs against a single page; generate proposes the whole tree; export
mirrors accepted pages into the repository as a PR.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_current_user, require_role
from domains.docs.schemas import (
    BulkEditRequest,
    CreateDocRequest,
    DocDTO,
    DocEditDTO,
    SetPinnedRequest,
    UpdateDocRequest,
)
from domains.docs.services import doc_service
from domains.runs.services.feature_specs import (
    draft_doc_page,
    verify_doc_page,
)
from domains.runs.services.lifecycle import LifecycleError
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1", tags=["docs"])


@router.get("/docs", operation_id="opensweep_list_docs")
async def list_docs(
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
) -> list[DocDTO]:
    await require_repo_in_org(repository_uid, user.org_uid)
    return await doc_service.list_docs(repository_uid)


@router.get("/docs/{uid}", operation_id="opensweep_get_doc")
async def get_doc(uid: str, user: UserDTO = Depends(get_current_user)) -> DocDTO:
    d = await doc_service.get_doc(uid)
    await require_repo_in_org(d.repository_uid, user.org_uid)
    return doc_service.doc_to_dto(d)


@router.post("/docs", operation_id="opensweep_create_doc")
async def create_doc(
    req: CreateDocRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> DocDTO:
    await require_repo_in_org(req.repository_uid, user.org_uid)
    d = await doc_service.create_doc(
        repository_uid=req.repository_uid,
        slug=req.slug,
        title=req.title,
        summary=req.summary,
        body=req.body,
        watch_paths=req.watch_paths,
        pinned=req.pinned,
        actor=user.uid,
    )
    return doc_service.doc_to_dto(d)


@router.put("/docs/{uid}", operation_id="opensweep_update_doc")
async def update_doc(
    uid: str, req: UpdateDocRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> DocDTO:
    existing = await doc_service.get_doc(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    d = await doc_service.update_doc(
        uid,
        title=req.title,
        summary=req.summary,
        body=req.body,
        watch_paths=req.watch_paths,
        actor=user.uid,
    )
    return doc_service.doc_to_dto(d)


@router.delete("/docs/{uid}", operation_id="opensweep_delete_doc")
async def delete_doc(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    d = await doc_service.get_doc(uid)
    await require_repo_in_org(d.repository_uid, user.org_uid)
    await doc_service.delete_doc(uid, actor=user.uid)
    return {"status": "deleted"}


@router.post(
    "/repositories/{repository_uid}/docs/reset", operation_id="opensweep_reset_docs"
)
async def reset_docs(
    repository_uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Destructive: delete the repository's entire doc tree (pages + edits).
    Anchored memories keep their content but lose their freshness anchor;
    Checked stamps stay as history."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await doc_service.reset_docs(repository_uid, actor=user.uid)


@router.post("/docs/{uid}/pin", operation_id="opensweep_pin_doc")
async def pin_doc(
    uid: str, req: SetPinnedRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> DocDTO:
    existing = await doc_service.get_doc(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    d = await doc_service.set_pinned(uid, pinned=req.pinned, actor=user.uid)
    return doc_service.doc_to_dto(d)


@router.post("/docs/{uid}/draft", operation_id="opensweep_draft_doc_page")
async def draft_page(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    d = await doc_service.get_doc(uid)
    await require_repo_in_org(d.repository_uid, user.org_uid)
    try:
        run = await draft_doc_page(doc_uid=uid, triggered_by=user.uid)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_uid": run.uid}


@router.post("/docs/{uid}/verify", operation_id="opensweep_verify_doc_page")
async def verify_page(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    d = await doc_service.get_doc(uid)
    await require_repo_in_org(d.repository_uid, user.org_uid)
    try:
        run = await verify_doc_page(doc_uid=uid, triggered_by=user.uid)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_uid": run.uid}


@router.post(
    "/repositories/{repository_uid}/docs/export",
    operation_id="opensweep_export_docs",
)
async def export_docs(
    repository_uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    await require_repo_in_org(repository_uid, user.org_uid)

    from domains.docs.services.repo_export import ExportError, export_docs_to_repo

    try:
        result = await export_docs_to_repo(
            repository_uid=repository_uid, actor=user.uid
        )
    except ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


# ---------- DocEdits ----------


@router.get("/doc-edits", operation_id="opensweep_list_doc_edits")
async def list_doc_edits(
    repository_uid: str = Query(...),
    status: str = "pending",
    user: UserDTO = Depends(get_current_user),
) -> list[DocEditDTO]:
    await require_repo_in_org(repository_uid, user.org_uid)
    return await doc_service.list_doc_edits(repository_uid, status=status)


async def _require_doc_edits_in_org(uids: list[str], org_uid: str) -> None:
    """404 if ANY doc-edit in the batch is outside the org (or missing)."""
    for uid in uids:
        e = await doc_service.get_doc_edit(uid)
        await require_repo_in_org(e.repository_uid, org_uid)


@router.post("/doc-edits/{uid}/accept", operation_id="opensweep_accept_doc_edit")
async def accept_doc_edit(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> DocDTO:
    e = await doc_service.get_doc_edit(uid)
    await require_repo_in_org(e.repository_uid, user.org_uid)
    return await doc_service.accept_doc_edit(uid, actor=user.uid)


@router.post("/doc-edits/{uid}/reject", operation_id="opensweep_reject_doc_edit")
async def reject_doc_edit(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> DocEditDTO:
    e = await doc_service.get_doc_edit(uid)
    await require_repo_in_org(e.repository_uid, user.org_uid)
    return await doc_service.reject_doc_edit(uid, actor=user.uid)


@router.post("/doc-edits/bulk-accept", operation_id="opensweep_bulk_accept_doc_edits")
async def bulk_accept_doc_edits(
    req: BulkEditRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    await _require_doc_edits_in_org(req.uids, user.org_uid)
    accepted: list[str] = []
    errors: dict[str, str] = {}
    for uid in req.uids:
        try:
            await doc_service.accept_doc_edit(uid, actor=user.uid)
            accepted.append(uid)
        except HTTPException as exc:
            errors[uid] = str(exc.detail)
    return {"accepted": accepted, "errors": errors}


@router.post("/doc-edits/bulk-reject", operation_id="opensweep_bulk_reject_doc_edits")
async def bulk_reject_doc_edits(
    req: BulkEditRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    await _require_doc_edits_in_org(req.uids, user.org_uid)
    rejected: list[str] = []
    errors: dict[str, str] = {}
    for uid in req.uids:
        try:
            await doc_service.reject_doc_edit(uid, actor=user.uid)
            rejected.append(uid)
        except HTTPException as exc:
            errors[uid] = str(exc.detail)
    return {"rejected": rejected, "errors": errors}
