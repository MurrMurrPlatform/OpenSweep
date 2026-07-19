"""Run lifecycle (PLATFORM_V3_DESIGN.md §2, §3).

queued → running → awaiting_input | failed | cancelled | limit_exceeded |
paused_quota. The lifecycle service is the only legal entry point for
creating + dispatching a Run's FIRST turn; follow-up turns run through
turn_service. It handles policy resolution, ceiling enforcement, adapter
selection, raw-artifact retention, and per-turn playbook hooks.

One-off runs (review/fix/implement/verify/ask/chat) pass their intent
directly; agent-driven dispatch (domains/agents/services/dispatch.py)
composes the intent and passes scheduled_agent_uid/agent_uid provenance.
Workspaces are NOT destroyed when a turn finishes — they live under the
sliding retention window so follow-up turns can continue (V3 §7).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import settings
from domains.execution.schemas import SandboxDTO
from domains.execution.services.sandbox_service import SandboxService

# Side-effect imports register the adapters in AdapterRegistry. Keep these
# imports here so any caller of the lifecycle service also pulls them in.
from domains.executors import claude_code as _adapter_claude_code  # noqa: F401
from domains.executors import cli_tracking as _adapter_cli_tracking  # noqa: F401
from domains.executors import internal_llm as _adapter_internal_llm  # noqa: F401
from domains.executors import manual as _adapter_manual  # noqa: F401
from domains.executors.base import AdapterRegistry, DispatchRequest
from domains.runs.models import RUN_SURFACES, Run
from domains.runs.schemas import (
    ExecutionMode,
    Executor,
    RunStatus,
    RunTrigger,
)
from domains.runs.services import playbooks as playbook_registry
from domains.runs.services import run_changes
from domains.runs.services import workspace as workspace_service
from domains.runs.services.quota_retry import next_retry_at
from domains.runs.services.run_events import append_event
from domains.docs.services.briefing import build_briefing
from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services.llm_provider_service import select_provider
from domains.platform_tools.complete_run import complete_run
from domains.repositories.models import Repository
from domains.repositories.services.repository_service import repository_to_dto
from domains.run_policies.services.policy_resolver import (
    PolicyViolation,
)
from domains.run_policies.services.policy_resolver import (
    resolve as resolve_policy,
)
from infrastructure.audit import write_audit
from infrastructure.kill_switch import KillSwitchActiveError, assert_runnable
from infrastructure.process_role import get_role
from logging_config import logger


class LifecycleError(RuntimeError):
    """Raised when a Run cannot be dispatched (policy, kill switch, etc.)."""


# Executors that launch a CLI subprocess with cwd = repo path. They get a
# throwaway `git clone` from GitHub so their tool calls only see tracked
# files (no .venv, no node_modules, no build artefacts). Internal_llm uses
# HTTP + platform read tools and doesn't need a working dir.
_CLI_SANDBOXED_EXECUTORS = {Executor.CLAUDE_CODE, Executor.CODEX, Executor.OPENCODE}


def dispatch_result_is_stale(run_status: str) -> bool:
    """Should a first-turn adapter's late result be DISCARDED rather than
    finalized? (Pure decision so the discard rule is unit-testable.)

    Discard only when an OUTSIDE actor terminated the run mid-flight — a human
    cancel/end or the reconciler failing it. Those paths fire the completion
    hook themselves, and re-finalizing would resurrect a dead run.

    `awaiting_input` is deliberately NOT stale: it means the agent
    self-completed this turn via the `complete_run` platform tool (CLI
    executors are told to call it). Treating that as stale would skip the
    write-gate push + draft PR that on_turn_complete performs, stranding the
    agent's commit in the sandbox. `running` (adapter finished without a
    self-report) is likewise not stale.
    """
    return run_status not in {
        RunStatus.RUNNING.value,
        RunStatus.AWAITING_INPUT.value,
    }


async def trigger_run(
    *,
    repository_uid: str = "",
    intent: str = "",
    playbook: str = "",
    title: str = "",
    target: dict[str, Any] | None = None,
    linked_pr_uid: str = "",
    linked_ticket_uid: str = "",
    linked_finding_uid: str = "",
    scheduled_agent_uid: str = "",
    agent_uid: str = "",
    agent_rev: int = 0,
    stage: str = "",
    executor: Executor | None = None,
    execution_mode: ExecutionMode | None = None,
    run_policy_uid: str | None = None,
    trigger: RunTrigger = RunTrigger.MANUAL,
    triggered_by: str = "",
    surface: str = "runs",
    wait_for_completion: bool = False,
    prepared_sandbox: SandboxDTO | None = None,
    sandbox_factory: Callable[[], Awaitable[SandboxDTO]] | None = None,
) -> Run:
    """Create a Run and dispatch its first turn.

    Callers pass repository_uid + intent (+ playbook/target/links).
    Agent-driven dispatch also passes scheduled_agent_uid / agent_uid /
    agent_rev provenance and an explicit workflow `stage` when the agent key
    carries a sharper signal than the playbook (generate-docs → discover).

    Returns the persisted Run immediately by default (status `queued`) while
    sandbox prep + the adapter continue in the background. Write runs pass a
    pre-made write sandbox or a sandbox_factory. Completion hooks are resolved
    from the run's playbook after every turn (fix/implement → write gate); a
    prep failure marks the run failed with usage["prep_failed"]=True so the
    hooks can tell "agent never ran" from a real run failure.
    """
    if not repository_uid:
        raise LifecycleError("trigger_run needs repository_uid")
    if not intent:
        raise LifecycleError("trigger_run needs an intent")
    playbook = playbook or "ask"
    if playbook not in playbook_registry.PLAYBOOKS:
        raise LifecycleError(f"unknown playbook {playbook!r}")
    surface = surface or "runs"
    if surface not in RUN_SURFACES:
        raise LifecycleError(f"unknown surface {surface!r}")
    target = dict(target or {})

    try:
        await assert_runnable(repository_uid)
    except KillSwitchActiveError as exc:
        raise LifecycleError(str(exc)) from exc

    # Tenancy: provider selection is restricted to the repository's org's
    # own providers.
    from domains.llm_providers.services.llm_provider_service import repository_org_uid

    run_org_uid = await repository_org_uid(repository_uid)

    # Agent provenance: which Agent (and org override revision) supplied the
    # instructions layer at dispatch. Callers that composed the intent
    # themselves (agents.dispatch, sweep) pass agent_uid/agent_rev
    # explicitly; otherwise resolve the playbook's system agent.
    # Best-effort — provenance must never block a run.
    if not agent_uid:
        try:
            from domains.agents.services.agent_service import active_agent_provenance

            agent_uid, agent_rev = await active_agent_provenance(
                run_org_uid or "", playbook
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"agent provenance lookup failed ({run_org_uid}/{playbook}): {exc}",
                extra={"tag": "lifecycle"},
            )

    # Per-stage workflow overrides (repo dashboard → Workflow card): a stage
    # may pin its own provider, model, and wall ceiling. Empty/0 = inherit.
    from domains.repositories.services import workflow as workflow_service

    stage = stage or workflow_service.stage_for_run("", playbook)
    overrides = (
        await workflow_service.stage_run_overrides(repository_uid, stage)
        if stage
        else {"provider_uid": "", "model": "", "max_wall_seconds": 0, "run_policy_uid": ""}
    )
    override_warnings: list[str] = []

    # Stage-pinned provider first; else the active provider, falling back to
    # the next healthy enabled provider in the §8 chain (fallback_priority).
    # A pinned provider must belong to the run's org — a cross-org (or
    # unowned) pin is ignored, never honoured.
    active_provider: LLMProvider | None = None
    if overrides["provider_uid"]:
        pinned = await LLMProvider.nodes.get_or_none(uid=overrides["provider_uid"])
        pinned_scope = (getattr(pinned, "org_uid", "") or "") if pinned else None
        if (
            pinned is not None
            and bool(getattr(pinned, "enabled", True))
            and run_org_uid
            and pinned_scope == run_org_uid
        ):
            active_provider = pinned
        else:
            override_warnings.append(
                f"workflow.{stage}: pinned provider {overrides['provider_uid']} "
                "not found or disabled — falling back to the active provider chain"
            )
    if active_provider is None:
        active_provider = await select_provider(org_uid=run_org_uid)
    if active_provider is None:
        raise LifecycleError(
            "No LLM provider configured for this organization. An org admin must add one in Settings → LLM Providers and mark it active."
        )
    chosen_executor = executor or _executor_for_provider(active_provider)
    # A normal run is read-only investigation work. Only write playbooks
    # (implement/fix, §6) pass an explicit execution_mode — and they must
    # bring their own write sandbox (made or deferred).
    chosen_mode = execution_mode or ExecutionMode.ANALYZE_ONLY
    if (
        chosen_mode != ExecutionMode.ANALYZE_ONLY
        and prepared_sandbox is None
        and sandbox_factory is None
    ):
        raise LifecycleError(
            f"execution_mode={chosen_mode.value} requires a prepared write sandbox"
        )

    try:
        # Policy precedence: the explicit per-run run_policy_uid wins (agent
        # dispatch resolves the binding's pin / effort there), then the
        # per-stage Workflow-card policy override, then the system default
        # (resolved downstream). A stage can thus pin a full ceiling bundle
        # (dollars/wall/turns/files) for its runs, not just the raw wall
        # seconds.
        default_policy_uid = overrides.get("run_policy_uid") or None
        resolved = await resolve_policy(
            repository_uid=repository_uid,
            executor=chosen_executor,
            trigger=trigger,
            run_policy_uid=run_policy_uid,
            default_policy_uid=default_policy_uid,
        )
    except PolicyViolation as exc:
        raise LifecycleError(f"policy: {exc}") from exc

    repo = await Repository.nodes.get_or_none(uid=repository_uid)

    context = await _load_briefing(
        repository_uid=repository_uid,
        target=target,
    )

    run_uid = uuid4().hex

    # CLI-based executors ALWAYS get a throwaway git clone from GitHub so
    # their tool calls only see tracked files. A CLI executor is never
    # dispatched without a working dir — sandbox prep failure fails the run.
    # The clone itself happens in the background pipeline below, never inside
    # the caller's request. Internal_llm keeps None.
    if (
        prepared_sandbox is None
        and sandbox_factory is None
        and chosen_executor in _CLI_SANDBOXED_EXECUTORS
        and repo is None
    ):
        raise LifecycleError(f"Repository {repository_uid} not found")

    now = datetime.now(UTC)
    base_branch = str(target.get("base_branch") or target.get("base_ref") or "")
    run = Run(
        uid=run_uid,
        repository_uid=repository_uid,
        playbook=playbook,
        title=title or f"{playbook} run",
        scheduled_agent_uid=scheduled_agent_uid or "",
        executor=chosen_executor.value,
        execution_mode=chosen_mode.value,
        run_policy_uid=resolved.policy.uid,
        provider_uid=(active_provider.uid or "").strip(),
        agent_uid=agent_uid,
        agent_rev=agent_rev,
        status=RunStatus.QUEUED.value,
        linked_pr_uid=linked_pr_uid or str(target.get("pull_request_uid") or ""),
        linked_ticket_uid=linked_ticket_uid or str(target.get("ticket_uid") or ""),
        linked_finding_uid=linked_finding_uid or str(target.get("finding_uid") or ""),
        target=target,
        sandbox_uid=prepared_sandbox.uid if prepared_sandbox else "",
        workspace_spec=(
            workspace_service.build_workspace_spec(prepared_sandbox, base_branch=base_branch)
            if prepared_sandbox
            else {}
        ),
        trigger=trigger.value,
        triggered_by=triggered_by or "",
        surface=surface or "runs",
        started_at=now,
        last_activity_at=now,
        usage={
            "warnings": resolved.warnings + override_warnings,
            # Which process owns the dispatch asyncio task — that process
            # fails this run at its next startup if it dies mid-run.
            "dispatch_runtime": get_role(),
            "provider_uid": (active_provider.uid or "").strip(),
            "provider_kind": (active_provider.kind or "").strip(),
            "provider_label": (active_provider.label or "").strip(),
            # Effective model for this run: the per-stage workflow override
            # wins over the provider's own model (mirrors adapter dispatch).
            "provider_model": (overrides["model"] or active_provider.model or "").strip(),
            # Per-stage workflow overrides applied to this run. The
            # reconciler prefers this max_wall_seconds over the policy's.
            "workflow_overrides": {
                "stage": stage,
                "provider_uid": overrides["provider_uid"],
                "model": overrides["model"],
                "max_wall_seconds": overrides["max_wall_seconds"],
                "run_policy_uid": overrides.get("run_policy_uid", ""),
            },
            "sandbox_uid": prepared_sandbox.uid if prepared_sandbox else "",
            "input": {
                "intent": intent,
                "target": target,
                "repository_local_path": (
                    prepared_sandbox.container_path if prepared_sandbox else None
                ),
            },
        },
    )
    await run.save()
    await write_audit(
        kind="run.started",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid=triggered_by or chosen_executor.value,
        payload={
            "playbook": playbook,
            "scheduled_agent_uid": scheduled_agent_uid or "",
            "executor": chosen_executor.value,
            "mode": chosen_mode.value,
            "policy_uid": resolved.policy.uid,
        },
    )

    adapter = AdapterRegistry.get(chosen_executor)
    pipeline = _prepare_dispatch_and_finalize(
        run_uid=run.uid,
        repository_uid=repository_uid,
        intent=intent,
        target=target,
        repo=repo,
        adapter=adapter,
        chosen_executor=chosen_executor,
        chosen_mode=chosen_mode,
        trigger=trigger,
        triggered_by=triggered_by,
        policy=resolved.policy,
        warnings=resolved.warnings + override_warnings,
        provider_uid=(active_provider.uid or "").strip(),
        model_override=overrides["model"],
        max_wall_seconds_override=int(overrides["max_wall_seconds"] or 0),
        context=context,
        prepared_sandbox=prepared_sandbox,
        sandbox_factory=sandbox_factory,
    )

    if wait_for_completion:
        await pipeline
        return await Run.nodes.get(uid=run.uid)

    task = asyncio.create_task(pipeline)

    def _log_task_failure(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"run dispatch task crashed for {run.uid}: {type(exc).__name__}: {exc}")

    task.add_done_callback(_log_task_failure)
    return run


async def _prepare_dispatch_and_finalize(
    *,
    run_uid: str,
    repository_uid: str,
    intent: str,
    target: dict[str, Any],
    repo: Repository | None,
    adapter,
    chosen_executor: Executor,
    chosen_mode: ExecutionMode,
    trigger: RunTrigger,
    triggered_by: str,
    policy,
    warnings: list[str],
    provider_uid: str,
    context: str,
    prepared_sandbox: SandboxDTO | None,
    sandbox_factory: Callable[[], Awaitable[SandboxDTO]] | None,
    model_override: str = "",
    max_wall_seconds_override: int = 0,
) -> None:
    """Background half of trigger_run: sandbox prep → running → dispatch.

    Runs after trigger_run has already returned the queued row, so the git
    clone never blocks the dispatching HTTP request."""
    sandbox_uid = ""
    local_path: str | None = None
    workspace_spec: dict[str, Any] = {}
    base_branch = str(target.get("base_branch") or target.get("base_ref") or "")
    try:
        if prepared_sandbox is not None:
            sandbox_uid, local_path = prepared_sandbox.uid, prepared_sandbox.container_path
            workspace_spec = workspace_service.build_workspace_spec(
                prepared_sandbox, base_branch=base_branch
            )
        elif sandbox_factory is not None:
            append_event(run_uid, "system", kind="sandbox", text="preparing write sandbox — cloning from GitHub")
            sandbox = await sandbox_factory()
            sandbox_uid, local_path = sandbox.uid, sandbox.container_path
            workspace_spec = workspace_service.build_workspace_spec(
                sandbox, base_branch=base_branch
            )
        elif chosen_executor in _CLI_SANDBOXED_EXECUTORS:
            append_event(run_uid, "system", kind="sandbox", text="preparing sandbox — cloning from GitHub")
            head_ref = str(target.get("head_ref") or "").strip()
            base_ref = str(target.get("base_ref") or "").strip()
            sandbox = await SandboxService().create_for_discovery(
                repository=repository_to_dto(repo),
                agent_run_uid=run_uid,
                source_branch=head_ref or None,
                extra_refs=[base_ref] if base_ref else None,
            )
            sandbox_uid, local_path = sandbox.uid, sandbox.container_path
            workspace_spec = workspace_service.build_workspace_spec(
                sandbox, base_branch=base_ref
            )
    except Exception as exc:  # noqa: BLE001
        await _fail_run_prep(run_uid, exc)
        return

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return
    if run.status != RunStatus.QUEUED.value:
        # Cancelled (or reconciled) while the clone ran — don't dispatch. The
        # fresh sandbox isn't recorded on the run yet, so destroy it directly.
        if sandbox_uid and prepared_sandbox is None and sandbox_factory is None:
            try:
                await SandboxService().destroy(sandbox_uid, actor_uid=run.executor)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"sandbox {sandbox_uid} cleanup failed for cancelled run {run.uid}: {exc}",
                    extra={"tag": "lifecycle"},
                )
        return
    now = datetime.now(UTC)
    usage = dict(run.usage or {})
    usage["sandbox_uid"] = sandbox_uid
    input_blob = dict(usage.get("input") or {})
    input_blob["repository_local_path"] = local_path
    usage["input"] = input_blob
    run.usage = usage
    run.sandbox_uid = sandbox_uid
    if workspace_spec:
        run.workspace_spec = workspace_spec
    run.status = RunStatus.RUNNING.value
    # Wall clock / duration measure execution, not the queued clone.
    run.started_at = now
    run.updated_at = now
    await run.save()

    context = await _maybe_run_static_analysis(
        run=run, repo=repo, local_path=local_path, context=context
    )

    req = DispatchRequest(
        run_uid=run_uid,
        scheduled_agent_uid=run.scheduled_agent_uid or "",
        repository_uid=repository_uid,
        repository_local_path=local_path,
        intent=intent,
        target=target,
        context=context,
        mode=chosen_mode,
        trigger=trigger,
        triggered_by=triggered_by,
        policy=policy,
        provider_uid=provider_uid,
        model_override=model_override,
        max_wall_seconds_override=max_wall_seconds_override,
    )
    await _dispatch_and_finalize(
        adapter=adapter,
        req=req,
        warnings=warnings,
    )


# Playbooks whose runs get deterministic static-analysis candidates appended
# to their context (§E). Write/verify/chat runs don't — candidates would only
# distract from their structural contracts.
_ANALYZED_PLAYBOOKS = {"review", "ask"}


async def _maybe_run_static_analysis(
    *, run: Run, repo: Repository | None, local_path: str | None, context: str
) -> str:
    """Run the repo's configured analyzers over the fresh sandbox and append
    a capped candidate section to the context. Best-effort by construction:
    any failure returns the context unchanged and never fails the run."""
    if (run.playbook or "") not in _ANALYZED_PLAYBOOKS or not local_path or repo is None:
        return context
    try:
        from domains.execution.services import static_analysis as sa
        from domains.repositories.services.analyzer_config import get_analyzers
        from infrastructure import artifact_store

        config = await get_analyzers(repo.uid)
        tools = sa.resolve_tools(config, local_path)
        if not tools:
            return context
        report = await sa.run_analyzers(workspace_path=local_path, tools=tools)

        # Scope: review runs care about the PR's changed files; audit/ask
        # runs about their target paths. No paths = whole workspace.
        target = dict(run.target or {})
        allowed: list[str] = []
        if run.playbook == "review" and target.get("base_ref") and target.get("head_ref"):
            allowed = await sa.diff_paths(
                local_path, str(target["base_ref"]), str(target["head_ref"])
            )
        elif target.get("paths"):
            allowed = [str(p) for p in target["paths"]]
        report.candidates = sa.filter_candidates(report.candidates, allowed_paths=allowed or None)

        artifact_uri = ""
        if report.candidates or report.raw_outputs:
            artifact_uri = artifact_store.put(
                repository_uid=repo.uid,
                run_uid=run.uid,
                content=sa.report_to_json(report),
                artifact_type="static_analysis",
                extension="json",
            )
        section = sa.render_candidates_section(report, artifact_uri=artifact_uri)
        if not section:
            return context

        counts = ", ".join(
            f"{t} {sum(1 for c in report.candidates if c.tool == t)}" for t in report.tools_run
        )
        skipped = ", ".join(s["tool"] for s in report.tools_skipped)
        append_event(
            run.uid,
            "system",
            kind="analysis",
            text=f"static analysis: {counts or 'nothing ran'}"
            + (f" (skipped: {skipped})" if skipped else ""),
        )
        usage = dict(run.usage or {})
        usage["static_analysis"] = {
            "uri": artifact_uri,
            "tools_run": report.tools_run,
            "tools_skipped": report.tools_skipped,
            "candidates": len(report.candidates),
            # Quota redispatch rebuilds context from the briefing — it
            # re-appends this instead of re-running analyzers.
            "summary_md": section,
        }
        run.usage = usage
        if artifact_uri:
            run.output_refs = list({*(run.output_refs or []), artifact_uri})
        await run.save()
        return f"{context}\n\n{section}" if context else section
    except Exception as exc:  # noqa: BLE001 — candidates are a bonus, never a blocker
        logger.warning(
            f"static analysis failed for run {run.uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "lifecycle"},
        )
        return context


async def _fail_run_prep(run_uid: str, exc: Exception) -> None:
    """Sandbox prep failed — the agent never ran. Fail the run with a
    `prep_failed` marker so the playbook hooks (e.g. the fix-round refund)
    can tell this apart from a real run failure."""
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return
    now = datetime.now(UTC)
    run.status = RunStatus.FAILED.value
    run.error = f"sandbox prep failed: {type(exc).__name__}: {exc}"[:500]
    run.completed_at = now
    if run.started_at and not run.duration_ms:
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    run.updated_at = now
    usage = dict(run.usage or {})
    usage["prep_failed"] = True
    run.usage = usage
    await run.save()
    await write_audit(
        kind="run.failed",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid=run.executor,
        payload={"error": run.error, "prep_failed": True},
    )
    append_event(run.uid, "error", detail=run.error)
    append_event(run.uid, "system", kind="run_status", text=f"run {run.status}")
    await playbook_registry.on_turn_complete(run)


async def _dispatch_and_finalize(
    *,
    adapter,
    req: DispatchRequest,
    warnings: list[str],
) -> None:
    run = await Run.nodes.get_or_none(uid=req.run_uid)
    if run is None:
        return
    try:
        result = await adapter.dispatch(req)
    except Exception as exc:  # noqa: BLE001
        run = await Run.nodes.get_or_none(uid=req.run_uid)
        if run is None or run.status != RunStatus.RUNNING.value:
            # Cancelled/reconciled while the adapter ran — the row already
            # tells the truth; don't clobber it (mirrors the check below).
            logger.info(
                f"run {req.run_uid}: adapter failure discarded — run is no longer "
                f"running (status={run.status if run else 'deleted'}): {exc}",
                extra={"tag": "lifecycle"},
            )
            return
        now = datetime.now(UTC)
        run.status = RunStatus.FAILED.value
        run.error = f"{type(exc).__name__}: {exc}"[:500]
        run.completed_at = now
        if run.started_at and not run.duration_ms:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        run.updated_at = now
        run.last_activity_at = now
        await run.save()
        await write_audit(
            kind="run.failed",
            subject_uid=run.uid,
            subject_type="Run",
            actor_uid=run.executor,
            payload={"error": run.error},
        )
        append_event(run.uid, "system", kind="run_status", text=f"run {run.status}")
        await playbook_registry.on_turn_complete(run)
        return

    # Re-read before finalizing: discard the adapter's result only if an
    # OUTSIDE actor terminated the run mid-flight (see dispatch_result_is_stale
    # — an agent self-completing via complete_run is NOT such a case, so its
    # write-gate push + draft PR still fire).
    fresh = await Run.nodes.get_or_none(uid=req.run_uid)
    if fresh is None:
        return
    if dispatch_result_is_stale(fresh.status):
        logger.info(
            f"run {req.run_uid}: dispatch result discarded — run was terminated "
            f"mid-flight (status={fresh.status})",
            extra={"tag": "lifecycle"},
        )
        if fresh.sandbox_uid and (fresh.execution_mode or "analyze_only") == "analyze_only":
            try:
                await SandboxService().destroy(fresh.sandbox_uid, actor_uid=fresh.executor)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"sandbox {fresh.sandbox_uid} cleanup failed for run {fresh.uid}: {exc}",
                    extra={"tag": "lifecycle"},
                )
        return

    # The manual executor returns RUNNING — the human will call complete_run
    # later via the HTTP transport.
    if result.status == RunStatus.RUNNING:
        return

    # Quota is a state, not a failure (§8): keep the sandbox ALIVE (the retry
    # reuses it), record the pause, and let the resume beat task re-dispatch.
    # Playbook hooks are NOT fired: the run is paused, not finished, so e.g. a
    # paused review simply keeps the PR not-converged instead of yielding a
    # stale merge.
    if result.status == RunStatus.PAUSED_QUOTA:
        await _pause_run_for_quota(run_uid=req.run_uid, result=result, warnings=warnings)
        append_event(req.run_uid, "system", kind="run_status", text=f"run {RunStatus.PAUSED_QUOTA.value}")
        return

    outcome = dict(result.outcome or {})
    await complete_run(
        run_uid=req.run_uid,
        summary=result.summary,
        did=outcome.get("did"),
        skipped=outcome.get("skipped"),
        succeeded=outcome.get("succeeded"),
        failed=outcome.get("failed"),
        next_steps=outcome.get("next_steps"),
        output_refs=list(result.output_refs or []),
        usage={**(result.usage or {}), **{"warnings": warnings}},
        raw_artifact_uri=result.raw_artifact_uri or None,
        parse_status=result.parse_status,
        error=result.error or None,
        final_status=result.status.value,
    )
    final = await Run.nodes.get_or_none(uid=req.run_uid) or run
    now = datetime.now(UTC)
    final.turns = int(final.turns or 0) + 1
    final.last_activity_at = now
    final.updated_at = now
    await final.save()
    append_event(final.uid, "system", kind="run_status", text=f"run {final.status}")
    # The workspace is NOT destroyed (V3 §7): it lives under the sliding
    # retention window so follow-up turns can continue in it.
    await workspace_service.touch_workspace(final)
    await run_changes.snapshot_changes(final)
    await playbook_registry.on_turn_complete(final)


async def _pause_run_for_quota(*, run_uid: str, result, warnings: list[str]) -> None:
    """Persist a quota pause (§8): status paused_quota + usage["quota"] record.

    The sandbox is deliberately left alive and its retention extended — the
    retry re-uses it. The resume beat task (tasks/resume_paused.py) picks the
    run up again after the reset window, or immediately when an unexhausted
    fallback provider exists.
    """
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return
    now = datetime.now(UTC)
    usage = dict(run.usage or {})
    # Platform-owned keys must survive the executor-usage merge: "input"
    # holds the original dispatch target and "quota" holds the retry ledger.
    # A result.usage that happens to carry either key would otherwise clobber
    # them.
    preserved_input = usage.get("input")
    preserved_quota = dict(usage.get("quota") or {})
    usage.update(result.usage or {})
    if preserved_input is not None:
        usage["input"] = preserved_input
    usage["warnings"] = warnings

    quota = preserved_quota
    provider_uid = (usage.get("provider_uid") or "").strip()
    exhausted = [u for u in (quota.get("exhausted_provider_uids") or []) if u]
    if provider_uid and provider_uid not in exhausted:
        exhausted.append(provider_uid)
    from domains.llm_providers.services.llm_provider_service import repository_org_uid

    fallback = await select_provider(
        org_uid=await repository_org_uid(run.repository_uid), exclude_uids=set(exhausted)
    )
    retry_count = int(quota.get("retry_count") or 0)
    quota.update(
        {
            "detected_at": now.isoformat(),
            "retry_count": retry_count,
            "provider_uid": provider_uid,
            "exhausted_provider_uids": exhausted,
            "fallback_available": fallback is not None,
            "next_retry_at": next_retry_at(
                detected_at=now,
                retry_minutes=int(settings.OPENSWEEP_QUOTA_RETRY_MINUTES),
                fallback_available=fallback is not None,
            ).isoformat(),
        }
    )
    usage["quota"] = quota
    run.usage = usage
    run.status = RunStatus.PAUSED_QUOTA.value
    run.error = (result.error or "provider quota/rate limit reached")[:500]
    if result.raw_artifact_uri:
        run.raw_artifact_uri = result.raw_artifact_uri
    if result.parse_status:
        run.parse_status = result.parse_status
    run.updated_at = now
    await run.save()
    # A workspace only survives under the sliding retention window; a quota
    # pause can wait far longer than a turn — push cleanup_after out.
    await workspace_service.touch_workspace(run)
    await write_audit(
        kind="run.paused_quota",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid=run.executor,
        payload={
            "provider_uid": provider_uid,
            "retry_count": retry_count,
            "exhausted_provider_uids": exhausted,
            "fallback_available": fallback is not None,
            "next_retry_at": quota["next_retry_at"],
        },
    )


async def redispatch_run(
    run: Run,
    *,
    exclude_provider_uids: set[str] | frozenset[str] = frozenset(),
) -> Run:
    """Re-dispatch the SAME Run after a quota pause (§8).

    Rebuilds the DispatchRequest from run.usage["input"] plus the run's
    workspace (recreated from workspace_spec when it is gone), picks a
    provider via the fallback chain excluding the quota-exhausted ones, and
    awaits the dispatch through the normal finalize path. Playbook hooks fire
    automatically on completion. Raises LifecycleError when no usable
    provider or workspace can be arranged.
    """
    try:
        await assert_runnable(run.repository_uid)
    except KillSwitchActiveError as exc:
        raise LifecycleError(str(exc)) from exc

    from domains.llm_providers.services.llm_provider_service import repository_org_uid

    chosen_mode = ExecutionMode(run.execution_mode or ExecutionMode.ANALYZE_ONLY.value)
    exclude = set(exclude_provider_uids)
    if chosen_mode == ExecutionMode.IMPLEMENT:
        # Write runs (fix/implement) work in a write sandbox and go through
        # the write gate — only executors with a write surface may resume
        # them. A read-only fallback (internal_llm/opencode/codex tracking)
        # would waste the round; leave the run paused (raise → the resume
        # task records the error and retries a later tick) instead.
        exclude |= {
            (p.uid or "").strip()
            for p in await LLMProvider.nodes.all()
            if not _provider_supports_write(p)
        }
    # Tenancy: restricted to the run's org's own providers.
    provider = await select_provider(
        org_uid=await repository_org_uid(run.repository_uid), exclude_uids=exclude
    )
    if provider is None:
        if chosen_mode == ExecutionMode.IMPLEMENT:
            raise LifecycleError(
                "no write-capable LLM provider available for quota retry — "
                "the run stays paused until one is usable"
            )
        raise LifecycleError("no usable LLM provider available for quota retry")
    chosen_executor = _executor_for_provider(provider)

    policy = None
    if run.run_policy_uid:
        from domains.run_policies.models import RunPolicy

        policy = await RunPolicy.nodes.get_or_none(uid=run.run_policy_uid)

    usage = dict(run.usage or {})
    input_blob = dict(usage.get("input") or {})
    target = dict(input_blob.get("target") or run.target or {})
    intent = str(input_blob.get("intent") or "")
    if not intent and run.scheduled_agent_uid:
        from domains.agents.models import Agent, ScheduledAgent

        sa = await ScheduledAgent.nodes.get_or_none(uid=run.scheduled_agent_uid)
        agent = await Agent.nodes.get_or_none(uid=sa.agent_uid) if sa else None
        intent = (agent.prompt if agent else "") or ""
    if not intent:
        raise LifecycleError(f"run {run.uid} has no recorded intent to retry")
    context = await _load_briefing(
        repository_uid=run.repository_uid,
        target=target,
    )
    # Re-append the persisted static-analysis section (§E) instead of
    # re-running analyzers on a quota retry.
    analysis_md = str(((run.usage or {}).get("static_analysis") or {}).get("summary_md") or "")
    if analysis_md:
        context = f"{context}\n\n{analysis_md}" if context else analysis_md

    local_path = await _ensure_run_workspace(run, executor=chosen_executor, target=target)

    # Re-apply the run's recorded workflow overrides. The wall ceiling always
    # carries over. The model override carries over when it wasn't tied to a
    # provider pin, or when the resume landed on the provider it was pinned
    # with — a fallback away from a pinned provider keeps its own model.
    wf_overrides = dict(usage.get("workflow_overrides") or {})
    wf_provider_pin = str(wf_overrides.get("provider_uid") or "")
    resumed_model_override = (
        str(wf_overrides.get("model") or "")
        if not wf_provider_pin or (provider.uid or "").strip() == wf_provider_pin
        else ""
    )

    now = datetime.now(UTC)
    quota = dict(usage.get("quota") or {})
    quota["retry_count"] = int(quota.get("retry_count") or 0) + 1
    quota["resumed_at"] = now.isoformat()
    usage["quota"] = quota
    # Re-stamp ownership: quota resumes run inside the Celery worker, not
    # the process that originally dispatched the run.
    usage["dispatch_runtime"] = get_role()
    usage["provider_uid"] = (provider.uid or "").strip()
    usage["provider_kind"] = (provider.kind or "").strip()
    usage["provider_label"] = (provider.label or "").strip()
    usage["provider_model"] = (resumed_model_override or provider.model or "").strip()
    usage["sandbox_uid"] = run.sandbox_uid or ""
    input_blob["repository_local_path"] = local_path
    usage["input"] = {**input_blob}
    run.usage = usage
    run.executor = chosen_executor.value
    run.provider_uid = (provider.uid or "").strip()
    run.status = RunStatus.RUNNING.value
    run.error = ""
    run.updated_at = now
    await run.save()
    await write_audit(
        kind="run.resumed",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid=chosen_executor.value,
        payload={
            "retry_count": quota["retry_count"],
            "provider_uid": usage["provider_uid"],
            "provider_kind": usage["provider_kind"],
            "executor": chosen_executor.value,
            "excluded_provider_uids": sorted(exclude_provider_uids),
        },
    )

    adapter = AdapterRegistry.get(chosen_executor)
    req = DispatchRequest(
        run_uid=run.uid,
        scheduled_agent_uid=run.scheduled_agent_uid or "",
        repository_uid=run.repository_uid,
        repository_local_path=local_path,
        intent=intent,
        target=target,
        context=context,
        mode=chosen_mode,
        trigger=RunTrigger(run.trigger or RunTrigger.MANUAL.value),
        triggered_by=run.triggered_by or "",
        policy=policy,
        provider_uid=(provider.uid or "").strip(),
        model_override=resumed_model_override,
        max_wall_seconds_override=int(wf_overrides.get("max_wall_seconds") or 0),
    )
    await _dispatch_and_finalize(
        adapter=adapter,
        req=req,
        warnings=list(usage.get("warnings") or []),
    )
    return await Run.nodes.get(uid=run.uid)


async def _ensure_run_workspace(
    run: Run, *, executor: Executor, target: dict[str, Any]
) -> str | None:
    """Reuse the run's workspace for a retry; recreate from workspace_spec
    when it is gone. Returns the container path; None for executors that need
    no working dir (internal_llm)."""
    path = await workspace_service.live_workspace_path(run)
    if path is not None:
        return path
    if dict(run.workspace_spec or {}):
        try:
            return await workspace_service.recreate_workspace(run)
        except workspace_service.WorkspaceError as exc:
            raise LifecycleError(str(exc)) from exc
    if executor in _CLI_SANDBOXED_EXECUTORS:
        # Run with no recorded spec (never got as far as sandbox prep) — fall
        # back to a fresh discovery clone of the recorded head_ref, and record
        # a spec for next time.
        repo = await Repository.nodes.get_or_none(uid=run.repository_uid)
        if repo is None:
            raise LifecycleError(f"Repository {run.repository_uid} not found")
        head_ref = str(target.get("head_ref") or "").strip()
        base_ref = str(target.get("base_ref") or "").strip()
        try:
            sandbox = await SandboxService().create_for_discovery(
                repository=repository_to_dto(repo),
                agent_run_uid=run.uid,
                source_branch=head_ref or None,
                extra_refs=[base_ref] if base_ref else None,
            )
        except Exception as exc:  # noqa: BLE001
            raise LifecycleError(f"sandbox prep failed: {type(exc).__name__}: {exc}") from exc
        run.sandbox_uid = sandbox.uid
        run.workspace_spec = workspace_service.build_workspace_spec(sandbox, base_branch=base_ref)
        await run.save()
        return sandbox.container_path
    return None


async def _load_briefing(
    *,
    repository_uid: str,
    target: dict[str, Any],
) -> str:
    """KNOWLEDGE_V3 briefing: pinned + targeted Docs verbatim, unpinned
    index, target-anchored memories — plus the comment threads on every data
    item the run targets (human comments are standing instructions). Rendered
    once here; executors insert it into their first-turn prompt as-is."""
    raw_target_docs = target.get("doc_uids") or target.get("doc_uid") or []
    if isinstance(raw_target_docs, str):
        raw_target_docs = [raw_target_docs]
    briefing = ""
    try:
        briefing = await build_briefing(
            repository_uid=repository_uid,
            target_doc_uids=[str(uid) for uid in raw_target_docs if uid],
        )
    except Exception as exc:  # noqa: BLE001 — a briefing failure must not block dispatch
        logger.warning(
            f"docs briefing failed for {repository_uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "lifecycle"},
        )
    try:
        from domains.comments.service import comment_briefing_for_target

        comment_context = await comment_briefing_for_target(target)
        if comment_context:
            briefing = f"{briefing}\n\n{comment_context}" if briefing else comment_context
    except Exception as exc:  # noqa: BLE001 — same contract: never block dispatch
        logger.warning(
            f"comment briefing failed for {repository_uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "lifecycle"},
        )
    return briefing


def _executor_for_provider(provider: LLMProvider) -> Executor:
    kind = (provider.kind or "").strip()
    if kind == "claude_subscription":
        return Executor.CLAUDE_CODE
    if kind == "codex_subscription":
        return Executor.CODEX
    if kind == "opencode":
        return Executor.OPENCODE
    if kind in {"claude_api", "openai_api", "mlx", "lmstudio", "ollama", "custom"}:
        return Executor.INTERNAL_LLM
    raise LifecycleError(
        f"Active provider kind '{kind}' is not supported for runs."
    )


# Executors whose adapters have a write surface (IMPLEMENT mode: edit, test,
# commit in a write sandbox). claude_code is the only one: internal_llm is
# HTTP + read tools, and the codex/opencode tracking adapters are deliberately
# read/report only — the write playbooks (fix/implement) always dispatch with
# Executor.CLAUDE_CODE.
_WRITE_CAPABLE_EXECUTORS = frozenset({Executor.CLAUDE_CODE})


def _provider_supports_write(provider: LLMProvider) -> bool:
    """Whether a run in IMPLEMENT mode may resume on this provider."""
    try:
        return _executor_for_provider(provider) in _WRITE_CAPABLE_EXECUTORS
    except LifecycleError:
        return False
