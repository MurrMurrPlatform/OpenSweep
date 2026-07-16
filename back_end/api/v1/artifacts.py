"""Read-only artifact routes.

Tenancy: artifact URIs are opensweep-artifact://<repository_uid>/<run_uid>/…
(infrastructure/artifact_store.py) — the embedded repository is org-checked
before the blob is served.
"""

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_current_user
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure import artifact_store

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


@router.get("", operation_id="opensweep_get_artifact")
async def get_artifact(uri: str, user: UserDTO = Depends(get_current_user)):
    # F8: the org check parses the repo uid with the SAME `_safe`-normalizing
    # parser the store uses to build the served path — checked identity and
    # served blob can no longer diverge.
    await require_repo_in_org(artifact_store.repository_uid_of(uri), user.org_uid)
    data = artifact_store.get(uri)
    if data is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return {
        "uri": uri,
        "text": data.decode("utf-8", errors="replace"),
        "bytes": len(data),
    }
