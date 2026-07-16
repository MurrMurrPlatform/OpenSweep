"""Platform-level config: global kill switch + dev reset."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import require_platform_admin
from config import settings  # F7: dev-reset's env guard referenced this un-imported
from domains.repositories.models import PlatformConfig
from domains.repositories.schemas import PlatformConfigDTO, SetKillSwitchRequest
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit
from infrastructure.kill_switch import set_global_kill_switch

router = APIRouter(prefix="/api/v1/platform-config", tags=["platform_config"])


_SINGLETON_UID = "singleton"


@router.get("", response_model=PlatformConfigDTO, operation_id="opensweep_get_platform_config")
async def get_config(
    user: UserDTO = Depends(require_platform_admin),  # F7: was ungated
) -> PlatformConfigDTO:
    cfg = await PlatformConfig.nodes.get_or_none(uid=_SINGLETON_UID)
    if cfg is None:
        return PlatformConfigDTO(global_kill_switch=False, updated_at=None)
    return PlatformConfigDTO(
        global_kill_switch=bool(cfg.global_kill_switch),
        updated_at=cfg.updated_at,
    )


@router.post(
    "/kill-switch",
    response_model=PlatformConfigDTO,
    operation_id="opensweep_set_global_kill_switch",
)
async def toggle_global_kill_switch(
    req: SetKillSwitchRequest,
    user: UserDTO = Depends(require_platform_admin),
) -> PlatformConfigDTO:
    active = await set_global_kill_switch(req.active)
    await write_audit(
        kind="platform.global_kill_switch_changed",
        subject_uid=_SINGLETON_UID,
        subject_type="PlatformConfig",
        actor_uid=user.uid,
        payload={"active": active},
    )
    return PlatformConfigDTO(
        global_kill_switch=active,
        updated_at=datetime.now(timezone.utc),
    )


class DevResetRequest(BaseModel):
    # Explicit confirmation string so a stray API call/UI click can't wipe
    # the database.
    confirm: str = ""


@router.post("/dev-reset", operation_id="opensweep_dev_reset")
async def dev_reset_endpoint(
    req: DevResetRequest,
    user: UserDTO = Depends(require_platform_admin),
) -> dict[str, Any]:
    """Full dev reset: wipe all derived state, keep configuration, and
    re-seed everything a fresh install has (system RunPolicy, prompt
    library bootstrap, workflow default prompts, per-repo conventions
    pages). THE canonical reset path — UI tooling must call this instead of
    deleting nodes itself, or seeding gets skipped."""
    # Never available outside a dev/local environment — don't even reveal it.
    if (settings.ENVIRONMENT or "").strip().lower() not in ("local", "dev", "development", "test"):
        raise HTTPException(status_code=404, detail="Not Found")
    if req.confirm != "RESET":
        raise HTTPException(
            status_code=422,
            detail='dev reset requires {"confirm": "RESET"}',
        )
    from infrastructure.dev_reset import dev_reset

    result = await dev_reset()
    await write_audit(
        kind="platform.dev_reset",
        subject_uid=_SINGLETON_UID,
        subject_type="PlatformConfig",
        actor_uid=user.uid,
        payload=result,
    )
    return result
