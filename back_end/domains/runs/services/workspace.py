"""Run workspaces — ensure / recreate / touch (PLATFORM_V3_DESIGN.md §7).

A Run records `workspace_spec` (purpose, branches) at sandbox creation so the
workspace can be rebuilt at any time after retention expiry:

- write runs: fresh clone with checkout_existing on the work branch — safe
  because the write gate pushed all validated commits to GitHub; un-pushed
  work in an expired sandbox is gone by design (it never passed the gate).
- discovery runs: fresh clone of the recorded source branch.

CLI resume does not survive recreation (claude's session state lives in the
old working directory): callers must clear `cli_session_id`; the next turn is
seeded from the transcript tail instead. A `system{kind: workspace_recreated}`
event makes the seam visible in the conversation.
"""

from __future__ import annotations

import os

from domains.execution.models import Sandbox
from domains.execution.schemas import SandboxDTO, SandboxStatus
from domains.execution.services.sandbox_service import SandboxService
from domains.runs.models import Run
from domains.runs.services.run_events import append_event
from domains.repositories.models import Repository
from domains.repositories.services.repository_service import repository_to_dto
from logging_config import logger


class WorkspaceError(RuntimeError):
    """Raised when a run's workspace cannot be (re)created."""


def build_workspace_spec(sandbox: SandboxDTO, *, base_branch: str = "") -> dict:
    return {
        "purpose": sandbox.purpose or "discovery",
        "source_branch": sandbox.source_branch or "",
        "work_branch": sandbox.sandbox_branch or "",
        "base_branch": base_branch or "",
    }


async def live_workspace_path(run: Run) -> str | None:
    """Container path of the run's live workspace, or None when gone."""
    if not run.sandbox_uid:
        return None
    sb = await Sandbox.nodes.get_or_none(uid=run.sandbox_uid)
    if (
        sb is None
        or sb.status in {SandboxStatus.DESTROYED.value, SandboxStatus.FAILED.value}
        or not sb.container_path
        or not os.path.isdir(sb.container_path)
    ):
        return None
    return sb.container_path


async def ensure_workspace(run: Run) -> str | None:
    """Return the run's workspace path, recreating it from workspace_spec if
    it was destroyed. Returns None for executors that need no working dir
    (a run with no recorded spec). Persists run field changes."""
    path = await live_workspace_path(run)
    if path is not None:
        return path
    spec = dict(run.workspace_spec or {})
    if not spec:
        return None
    return await recreate_workspace(run)


async def recreate_workspace(run: Run) -> str:
    """Rebuild the workspace from Run.workspace_spec (V3 §7)."""
    spec = dict(run.workspace_spec or {})
    if not spec:
        raise WorkspaceError(f"run {run.uid} has no workspace_spec to recreate from")
    repo = await Repository.nodes.get_or_none(uid=run.repository_uid)
    if repo is None:
        raise WorkspaceError(f"Repository {run.repository_uid} not found")
    repo_dto = repository_to_dto(repo)
    service = SandboxService()

    purpose = str(spec.get("purpose") or "discovery")
    try:
        if purpose == "write":
            work_branch = str(spec.get("work_branch") or "").strip()
            if not work_branch:
                raise WorkspaceError("write workspace_spec has no work_branch")
            sandbox = await service.create_for_write(
                repository=repo_dto,
                agent_run_uid=run.uid,
                work_branch=work_branch,
                base_branch=str(spec.get("base_branch") or "") or None,
                checkout_existing=True,  # continue the pushed branch, never fork
            )
        else:
            sandbox = await service.create_for_discovery(
                repository=repo_dto,
                agent_run_uid=run.uid,
                source_branch=str(spec.get("source_branch") or "") or None,
                extra_refs=[spec["base_branch"]] if spec.get("base_branch") else None,
            )
    except WorkspaceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceError(
            f"workspace recreation failed: {type(exc).__name__}: {exc}"
        ) from exc

    run.sandbox_uid = sandbox.uid
    # CLI resume does not survive recreation — reseed from the transcript tail.
    run.cli_session_id = ""
    await run.save()
    append_event(
        run.uid,
        "system",
        kind="workspace_recreated",
        text=f"workspace recreated from {purpose} spec"
        + (f" (branch {spec.get('work_branch') or spec.get('source_branch')})" if spec else ""),
    )
    return sandbox.container_path


async def touch_workspace(run: Run) -> None:
    """Slide the retention window on every turn (best-effort)."""
    if not run.sandbox_uid:
        return
    try:
        await SandboxService().touch(run.sandbox_uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"workspace touch failed for run {run.uid}: {exc}", extra={"tag": "workspace"}
        )
