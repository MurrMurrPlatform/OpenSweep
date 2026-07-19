"""Shared dispatch/finalize machinery for the delivery run services.

The four run services (review / verify / implement / fix) repeat the same
skeletons; this module is their single home:

- ``dispatch_serialized`` — in-flight guard (blocking_run → 409) + the actual
  dispatch, serialized behind a per-target asyncio.Lock. The guard read and
  the run-row write inside ``trigger_run`` are separated by many awaits, so
  without the lock two concurrent dispatches for the same PR/ticket could
  both pass the guard and race two write runs onto one branch (same pattern
  as turn_service._SEND_LOCKS).
- ``require_repository`` — repository 404 / GitHub-coordinates 400 checks.
- ``finalize_write_run`` — the shared write-run finalize flow (fix /
  implement): sandbox lookup → failed-run audit → write gate →
  blocked audit, or push + per-service post-push action.
- ``record_write_gate_result`` — persists the gate outcome on the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import HTTPException

from domains.delivery.services import write_gate
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.execution.schemas import SandboxDTO
from domains.runs.models import Run
from domains.runs.schemas import RunStatus
from domains.runs.services.active_runs import (
    active_runs_for,
    blocking_run,
    conflict_detail,
)
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from infrastructure.git_providers import get_git_credentials
from logging_config import logger

# Per-process, per-target locks. Entries are never evicted — the population
# (PRs/tickets under active work) is small and locks are tiny.
_DISPATCH_LOCKS: dict[str, asyncio.Lock] = {}


# Shared write-run intent section (KNOWLEDGE_V3_DOCUMENTATION.md §9): every
# code-changing playbook tells the agent to keep the repo's Documentation and
# Memories true to what it touched. The briefing already inlines/indexes the
# pages; these are the write-back tools (opensweep_platform_* — the CLI executors'
# MCP mount). Appended by build_implement_intent / build_fix_intent.
DOC_UPKEEP_INTENT_SECTION = (
    "## Documentation & memory (OpenSweep platform tools — mandatory)\n"
    "Keep the repository's documentation true to the code you touch:\n"
    "- BEFORE editing, consult the docs for the areas you'll change: the briefing\n"
    "  above inlines the pinned/targeted pages and indexes the rest — pull any\n"
    "  relevant page with `opensweep_platform_read_doc`, and search durable facts with\n"
    "  `opensweep_platform_read_search_memory`.\n"
    "- AFTER the change is complete (before you finish): for each Documentation\n"
    "  page whose subject you changed, call `opensweep_platform_propose_doc_edit` with\n"
    "  the corrected full body — or `opensweep_platform_confirm_doc_current` if the page\n"
    "  is still accurate. If you changed an area that has no page yet and it\n"
    "  warrants one, propose a new page (fresh slug + title + watch_paths).\n"
    "- Record any durable, non-obvious fact you learned with\n"
    "  `opensweep_platform_write_memory` (one short paragraph — never something\n"
    "  derivable from the code itself).\n"
)


async def dispatch_serialized(
    *,
    target_uid: str,
    playbook: str,
    conflict_message: str,
    active_filter: dict[str, str],
    dispatch: Callable[[], Awaitable[Run]],
) -> Run:
    """Run the in-flight guard and ``dispatch()`` atomically per target.

    ``active_filter`` is the kwargs for ``active_runs_for`` (e.g.
    ``{"pull_request_uid": pr.uid}``); a conflicting active run raises the
    standard 409 with ``conflict_detail``. The lock keys on the linked
    entity uid so concurrent dispatches for the SAME PR/ticket serialize —
    the second one then sees the first's queued run and 409s.
    """
    lock = _DISPATCH_LOCKS.setdefault(target_uid, asyncio.Lock())
    async with lock:
        conflict = blocking_run(await active_runs_for(**active_filter), playbook=playbook)
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(conflict_message, conflict),
            )
        return await dispatch()


async def require_repository(repository_uid: str, *, require_github: bool = False) -> Repository:
    """The repo 404 (and optional GitHub-coordinates 400) trigger precheck."""
    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repository {repository_uid} not found")
    if require_github and not (repo.github_owner and repo.github_repo):
        raise HTTPException(status_code=400, detail="repository has no GitHub coordinates")
    return repo


async def record_write_gate_result(run_uid: str, result: write_gate.WriteGateResult) -> None:
    """Persist the gate outcome on the run so the UI/audit trail can show it."""
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return
    usage = dict(run.usage or {})
    usage["write_gate"] = result.to_dict()
    run.usage = usage
    run.updated_at = datetime.now(UTC)
    await run.save()


async def finalize_write_run(
    run: Run,
    *,
    audit_prefix: str,
    subject_uid: str,
    subject_type: str,
    repository_uid: str,
    base_ref: str,
    work_branch: str,
    on_pushed: Callable[[SandboxDTO, write_gate.WriteGateResult], Awaitable[None]],
    quiet_when_unchanged: bool = False,
) -> None:
    """Shared per-turn write-run finalize (V3 §3): validate the sandbox's new
    commits against ``base_ref`` → block (audited) or push ``work_branch`` →
    per-service post-push action. Callers resolve their linked entity and
    handle the prep_failed short-circuit BEFORE calling this.

    ``quiet_when_unchanged``: a turn whose only "violation" is having no
    commits returns silently instead of auditing as blocked — thread runs
    finalize every conversational turn (unified dev flow rev2).
    """
    # Local imports: execution service siblings would otherwise form an
    # import cycle through the delivery services package.
    from domains.execution.models import Sandbox
    from domains.execution.services.sandbox_service import sandbox_to_dto

    sb = await Sandbox.nodes.get_or_none(uid=run.sandbox_uid) if run.sandbox_uid else None
    if sb is None or not work_branch:
        logger.warning(
            f"{audit_prefix} {run.uid} has no workspace/branch — skipping finalize",
            extra={"tag": "delivery"},
        )
        return
    sandbox = sandbox_to_dto(sb)

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    default_branch = (repo.default_branch if repo else None) or "main"

    if run.status != RunStatus.AWAITING_INPUT.value:
        # Run failed — keep the workspace around for a human to inspect.
        await write_audit(
            kind=f"{audit_prefix}.failed",
            subject_uid=subject_uid,
            subject_type=subject_type,
            actor_uid="system",
            payload={"run_uid": run.uid, "sandbox_uid": sandbox.uid, "error": run.error or ""},
        )
        return

    policy = await ensure_merge_policy(repository_uid)
    result = await write_gate.validate_sandbox_changes(
        sandbox.container_path,
        base_ref=base_ref,
        policy=policy,
        default_branch=default_branch,
    )
    await record_write_gate_result(run.uid, result)

    if not result.ok:
        if quiet_when_unchanged and write_gate.is_only_no_commits(result.violations):
            return  # conversational turn — nothing to push, nothing to audit
        # NO push. Workspace retained for inspection.
        await write_audit(
            kind=f"{audit_prefix}.blocked",
            subject_uid=subject_uid,
            subject_type=subject_type,
            actor_uid="system",
            payload={
                "run_uid": run.uid,
                "sandbox_uid": sandbox.uid,
                "violations": result.violations,
                "changed_paths": result.changed_paths[:50],
            },
        )
        return

    # Never force-push; the branch is the agent's only write surface.
    await write_gate.push_work_branch(
        sandbox.container_path,
        work_branch=work_branch,
        token=await get_git_credentials(repo),
        default_branch=default_branch,
    )
    await on_pushed(sandbox, result)

    # Tie THIS run's changed paths to doc upkeep the moment it pushes
    # (KNOWLEDGE_V3_DOCUMENTATION.md §9): mark watching pages stale and
    # auto-run any on-event doc bindings the dial permits — without
    # waiting for GitHub to redeliver the push webhook. Same machinery the
    # webhook uses, so gating/dedup stay consistent; best-effort, never
    # blocks the finalize.
    from domains.agents.services.event_triggers import refresh_docs_for_change

    await refresh_docs_for_change(
        repository_uid=repository_uid,
        changed_paths=result.changed_paths,
        source="write-run",
    )
    # V3 §7: the workspace is NOT destroyed — it lives under the sliding
    # retention window so follow-up turns can continue on the branch.
