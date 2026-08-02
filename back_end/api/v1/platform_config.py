"""Platform-level config: global kill switch + dev reset."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.dependencies import require_platform_admin
from config import settings  # F7: dev-reset's env guard referenced this un-imported
from domains.repositories.models import PlatformConfig
from domains.repositories.schemas import PlatformConfigDTO, SetKillSwitchRequest
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit
from logging_config import logger
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


class ReseedRequest(BaseModel):
    """FORCE re-seed of platform content.

    Confirmation is explicit because FORCE is destructive to intent: it
    overwrites every platform-owned row, including ones a user deliberately
    edited. SYNC — what boot runs — cannot do this by design, and that is
    the whole problem it leaves behind.
    """

    confirm: str = ""
    # Seeder names to force ("lenses", "variant_prompts", "agent_base_prompts",
    # "workflow_default_prompts", …). Empty = every PLATFORM seeder.
    names: list[str] = Field(default_factory=list)


@router.post("/reseed", operation_id="opensweep_force_reseed")
async def force_reseed_endpoint(
    req: ReseedRequest,
    user: UserDTO = Depends(require_platform_admin),
) -> dict[str, Any]:
    """FORCE-re-seed platform content onto rows a user has edited.

    Boot runs SYNC, which rolls a shipped improvement forward ONLY onto rows
    it can prove nobody touched (`seed_checksum` still matches the stored
    content). That is the right default and it leaves a real hole: once a row
    is edited — or once a field is added or removed and every row's checksum
    changes wholesale — shipped improvements to that row never arrive again,
    and there was no way to say "yes, take mine".

    This has now cost two deploys: the m0015 lens-checksum change made every
    existing Lens look org-edited, and the PR #45 prompt bodies could not
    reach any repo that had tuned them. Both needed a FORCE pass that existed
    only as an enum value with no caller.

    Operator-gated and never automatic: this DISCARDS local edits to the
    seeders it runs. Scope it with `names` to avoid overwriting more than you
    mean to.
    """
    if req.confirm != "FORCE":
        raise HTTPException(
            status_code=422,
            detail=(
                'a force re-seed overwrites user-edited platform rows; '
                'it requires {"confirm": "FORCE"}'
            ),
        )
    from infrastructure.seeding import SeedMode, run_seeders, summarize

    names = [n.strip() for n in (req.names or []) if n.strip()]
    results = await run_seeders(SeedMode.FORCE, names=names or None)
    payload = {
        name: {
            "created": r.created,
            "updated": r.updated,
            "unchanged": r.unchanged,
            "preserved": r.preserved,
            "error": r.error or "",
        }
        for name, r in results.items()
    }
    logger.info(f"FORCE re-seed by {user.uid}: {summarize(results)}", extra={"tag": "seeding"})
    await write_audit(
        kind="platform.force_reseed",
        subject_uid=_SINGLETON_UID,
        subject_type="PlatformConfig",
        actor_uid=user.uid,
        payload={"names": names, "results": payload},
    )
    return payload
