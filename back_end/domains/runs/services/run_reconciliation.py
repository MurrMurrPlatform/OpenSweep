"""Repair for orphaned/stuck Runs — the persisted row must never lie forever.

Dispatch is an in-process asyncio task (the FastAPI backend for API
dispatches, a Celery worker for schedule ticks and quota resumes). If that
process restarts or crashes mid-run, nothing is left to move the row out of
`queued`/`running`. Three repair paths:

  - reconcile_orphaned_runs — startup sweep: the restarting process
    immediately fails the runs it owned (usage["dispatch_runtime"]).
  - reconcile_stale_runs — the graph-wide sweep, driven by the 5-minute
    Celery beat tick and nothing else: liveness first — the transcript
    stream going silent covers EVERY provider kind, including local ones
    that have no wall ceiling — then the wall ceiling for metered kinds.
  - reconcile_runs — the same staleness test over a caller-supplied list.
    Callers holding rows they already fetched (the in-flight 409 guard, a
    run-detail read) repair just those instead of scanning the graph.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings
from domains.runs.models import Run
from domains.runs.services import playbooks as playbook_registry
from domains.runs.services.run_events import append_event, events_path
from domains.llm_providers.services.llm_executor import is_local_provider_kind
from domains.run_policies.models import RunPolicy
from infrastructure.audit import write_audit

_REPAIRABLE_STATUSES = frozenset({"queued", "running"})


def last_activity(run: Any) -> datetime | None:
    """Most recent liveness signal for a run.

    The transcript file is appended on every streamed chunk / tool call and
    lives on the artifacts volume shared between backend and worker, so its
    mtime is the finest-grained signal; the DB timestamps only move at turn
    boundaries and serve as fallback.
    """
    candidates: list[datetime] = []
    try:
        mtime = events_path(run.uid).stat().st_mtime
        candidates.append(datetime.fromtimestamp(mtime, tz=timezone.utc))
    except OSError:
        pass
    for value in (run.last_activity_at, run.started_at, run.created_at):
        if value is not None:
            candidates.append(value)
    return max(candidates) if candidates else None


def stale_reason(
    *,
    now: datetime,
    started: datetime,
    last_seen: datetime,
    wall_ceiling_seconds: int | None,
    liveness_timeout_seconds: int,
    grace_seconds: int,
) -> str | None:
    """Why a queued/running run should be failed, or None while it may still
    be alive. `wall_ceiling_seconds` is None for local providers — liveness
    is their only backstop."""
    if now - last_seen >= timedelta(seconds=liveness_timeout_seconds):
        return (
            f"Run marked failed after {liveness_timeout_seconds}s without executor "
            f"activity. The dispatching process likely restarted or crashed mid-run."
        )
    if wall_ceiling_seconds is not None and now - started >= timedelta(
        seconds=wall_ceiling_seconds + grace_seconds
    ):
        return (
            f"Run marked failed after exceeding wall-time ceiling "
            f"({wall_ceiling_seconds}s + {grace_seconds}s grace). "
            f"The backend process may have restarted."
        )
    return None


def is_orphan(
    *,
    owner: str,
    role: str,
    last_seen: datetime | None,
    now: datetime,
    recent_activity_grace_seconds: int,
    single_replica: bool = True,
) -> bool:
    """Whether a queued/running run belongs to the role that just restarted.

    Runs missing the dispatch_runtime stamp (rows predating it) are orphaned
    only when their transcript has also gone quiet, so a live run owned by
    the OTHER role is never killed during the deploy window that introduces
    the stamp.

    ``single_replica`` (default) means one process per role: a run stamped with
    THIS role is dead the instant this process restarts, so it is failed
    immediately. With 2+ replicas of a role, a same-role run may be a LIVE
    sibling's — so fall back to the same liveness gate as unstamped runs and
    reap it only once its transcript has gone quiet.
    """
    if owner == role and single_replica:
        return True
    if owner and owner != role:
        return False
    # owner unknown, OR owner == role under multi-replica: liveness decides.
    if last_seen is None:
        return True
    return now - last_seen >= timedelta(seconds=recent_activity_grace_seconds)


async def reconcile_stale_runs(*, grace_seconds: int = 90) -> int:
    """Fail every dead queued/running run in the graph.

    This scans all repairable rows across all tenants, so its only caller is
    the 5-minute Celery beat tick; it must never run on a request path. See
    reconcile_runs for the variant request handlers use.
    """
    return await reconcile_runs(
        await Run.nodes.filter(status__in=list(_REPAIRABLE_STATUSES)),
        grace_seconds=grace_seconds,
    )


async def reconcile_runs(runs: Iterable[Any], *, grace_seconds: int = 90) -> int:
    """Fail the dead runs among ``runs``, leaving the live ones untouched.

    Repairs in place: a failed run's in-memory object comes back with
    status "failed", so callers can re-filter the list they passed in.
    """
    now = datetime.now(timezone.utc)
    liveness_timeout = int(settings.OPENSWEEP_RUN_LIVENESS_TIMEOUT_SECONDS)
    changed = 0
    # Per-policy wall ceilings are cached for the duration of one pass — one
    # RunPolicy fetch per policy, not per run.
    ceiling_cache: dict[str, int] = {}
    for run in runs:
        if run.status not in _REPAIRABLE_STATUSES:
            continue
        started = run.started_at or run.created_at
        if started is None:
            continue
        provider_kind = (run.usage or {}).get("provider_kind", "")
        # A per-stage workflow override recorded on the run outranks both the
        # policy ceiling and the local-provider skip (the executor enforced
        # that same override).
        override = int(
            ((run.usage or {}).get("workflow_overrides") or {}).get("max_wall_seconds") or 0
        )
        if override:
            ceiling: int | None = override
        elif is_local_provider_kind(provider_kind):
            ceiling = None
        else:
            policy_key = run.run_policy_uid or ""
            if policy_key not in ceiling_cache:
                ceiling_cache[policy_key] = await _wall_ceiling_seconds(run.run_policy_uid)
            ceiling = ceiling_cache[policy_key]
        reason = stale_reason(
            now=now,
            started=started,
            last_seen=last_activity(run) or started,
            wall_ceiling_seconds=ceiling,
            liveness_timeout_seconds=liveness_timeout,
            grace_seconds=grace_seconds,
        )
        if reason is None:
            continue
        await _fail_run(
            run,
            now=now,
            error=reason,
            audit_kind="run.reconciled_failed",
            usage_flag="reconciled_stale",
            payload={
                "wall_ceiling_seconds": ceiling,
                "liveness_timeout_seconds": liveness_timeout,
                "grace_seconds": grace_seconds,
            },
        )
        changed += 1
    return changed


async def reconcile_orphaned_runs(
    *, role: str, recent_activity_grace_seconds: int = 120
) -> int:
    """Startup sweep: fail the queued/running runs this process role owned.

    Dispatch tasks die with their process, so when a role starts up, every run
    still stamped with that role is dead by definition — UNDER a single-replica
    deployment. With OPENSWEEP_SINGLE_REPLICA=False (2+ replicas of a role), a
    same-role run may belong to a live sibling, so the sweep gates on transcript
    liveness instead of reaping same-role runs on sight — a replica never kills
    a sibling's in-flight run.
    """
    single_replica = bool(getattr(settings, "OPENSWEEP_SINGLE_REPLICA", True))
    now = datetime.now(timezone.utc)
    changed = 0
    # Cross-tenant reconciliation must sweep every repo's runs — but only
    # queued/running rows are repairable, so push the status gate down to the
    # DB instead of loading the whole Run table on every restart.
    for run in await Run.nodes.filter(status__in=list(_REPAIRABLE_STATUSES)):
        owner = str((run.usage or {}).get("dispatch_runtime") or "")
        if not is_orphan(
            owner=owner,
            role=role,
            last_seen=last_activity(run),
            now=now,
            recent_activity_grace_seconds=recent_activity_grace_seconds,
            single_replica=single_replica,
        ):
            continue
        await _fail_run(
            run,
            now=now,
            error=f"Run marked failed: the {role} process restarted while the run was in flight.",
            audit_kind="run.orphaned_on_restart",
            usage_flag="reconciled_orphaned",
            payload={"role": role, "dispatch_runtime": owner},
        )
        changed += 1
    return changed


async def _fail_run(
    run: Run,
    *,
    now: datetime,
    error: str,
    audit_kind: str,
    usage_flag: str,
    payload: dict[str, Any],
) -> None:
    run.status = "failed"
    run.completed_at = now
    run.error = error
    if run.started_at and not run.duration_ms:
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    run.updated_at = now
    usage = dict(run.usage or {})
    usage.setdefault("warnings", [])
    usage[usage_flag] = True
    run.usage = usage
    await run.save()
    await write_audit(
        kind=audit_kind,
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid="run_reconciliation",
        payload=payload,
    )
    append_event(run.uid, "error", detail=error)
    append_event(run.uid, "system", kind="run_status", text="run failed")
    # Fire the playbook completion hook so linked entities never wedge on a
    # killed run (e.g. a verify run's verdict stuck at verification_status=
    # "pending" forever). The hooks tolerate non-awaiting_input statuses and
    # never raise.
    await playbook_registry.on_turn_complete(run)


async def _wall_ceiling_seconds(policy_uid: str | None) -> int:
    from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS

    if policy_uid:
        p = await RunPolicy.nodes.get_or_none(uid=policy_uid)
        if p is not None and p.max_wall_seconds:
            return int(p.max_wall_seconds)
    return DEFAULT_MAX_WALL_SECONDS
