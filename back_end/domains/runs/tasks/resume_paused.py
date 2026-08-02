"""Celery tasks — resume quota-paused Runs (§8).

Two tasks:
  - `opensweep.runs.resume_paused_runs` (beat, every 10 min): ONLY
    selects runs with status `paused_quota` and enqueues one
    `opensweep.runs.resume_run` per run. It never re-dispatches
    anything itself — a re-dispatched CLI run can take an hour, far beyond
    the global 600/900s task limits that apply to the beat tick.
  - `opensweep.runs.resume_run` (per run, limits derived from the run's
    policy wall ceiling — see tasks/task_limits.py):
    - EXHAUSTED (retry_count >= OPENSWEEP_QUOTA_MAX_RETRIES) → fail for real
      ("quota retries exhausted"), destroy the discovery sandbox;
    - RETRY (an unexhausted fallback provider exists → immediately; otherwise
      once the OPENSWEEP_QUOTA_RETRY_MINUTES reset window has passed) →
      re-dispatch the SAME run via lifecycle.redispatch_run;
    - WAIT → leave the run paused until a later tick.

V3: completion hooks are resolved from the run's playbook inside the
lifecycle after every dispatch — nothing needs rebuilding here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from celery_app import app
from domains.runs.tasks.task_limits import (
    DEFAULT_DISPATCH_SECONDS,
    dispatch_time_limits,
    limits_for_run,
)
from logging_config import logger

_FALLBACK_SOFT, _FALLBACK_HARD = dispatch_time_limits(DEFAULT_DISPATCH_SECONDS)


@app.task(name="opensweep.runs.resume_paused_runs")
def resume_paused_runs() -> dict:
    from infrastructure.celery_async import run_async_task
    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()
    out = run_async_task(_scan_and_enqueue)
    logger.info(f"resume paused runs: {out}", extra={"tag": "quota"})
    return out


async def _scan_and_enqueue() -> dict:
    """Select eligible runs and fan out one resume_run task per run."""
    from domains.runs.models import Run

    paused = [r for r in await Run.nodes.all() if r.status == "paused_quota"]
    for run in paused:
        # A resumed run re-executes from the original intent, so it needs the
        # same headroom as a first dispatch — a `deep` run resumed under the
        # flat fallback would be killed at the old 3900s.
        soft, hard = await limits_for_run(run)
        resume_run.apply_async(
            args=[run.uid], soft_time_limit=soft, time_limit=hard
        )
    return {"scanned": len(paused), "enqueued": len(paused)}


@app.task(
    name="opensweep.runs.resume_run",
    soft_time_limit=_FALLBACK_SOFT,
    time_limit=_FALLBACK_HARD,
)
def resume_run(run_uid: str) -> dict:
    """Resume ONE quota-paused run — may execute a full CLI run, hence the
    per-task limits overriding the global 600/900s. `_scan_and_enqueue`
    passes policy-derived limits per invocation; these are the fallback."""
    from infrastructure.celery_async import run_async_task
    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()

    async def _go() -> dict:
        return await _resume_by_uid(run_uid)

    out = run_async_task(_go)
    logger.info(f"resume run {run_uid}: {out}", extra={"tag": "quota"})
    return out


async def _resume_by_uid(run_uid: str) -> dict:
    from domains.runs.models import Run
    from infrastructure.dist_lock import dist_lock

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None or run.status != "paused_quota":
        # Resolved/failed/resumed between scan and execution — nothing to do.
        return {"outcome": "skipped"}

    # Lease the run for the whole resume. `redispatch_run` only flips the row
    # off `paused_quota` AFTER re-cloning the workspace, so without a lease the
    # next 10-min beat tick would find it still paused and enqueue a SECOND
    # resume_run — a double dispatch (two agents, two clones) on one run. The
    # lease is cross-process (backend + every worker replica). TTL covers the
    # run's wall ceiling + slack so a crashed holder's lease expires and a later
    # tick retries rather than the run being stranded.
    _soft, hard = await limits_for_run(run)
    async with dist_lock(
        f"resume:{run_uid}", ttl_seconds=hard + 300, blocking=False
    ) as leased:
        if not leased:
            # Another resume for this run is already in flight.
            return {"outcome": "skipped"}
        # Re-read under the lease: the in-flight resume may have flipped it since
        # the pre-lease check above.
        run = await Run.nodes.get_or_none(uid=run_uid)
        if run is None or run.status != "paused_quota":
            return {"outcome": "skipped"}
        try:
            outcome = await _resume_one(run, now=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — recorded, next tick retries
            logger.warning(
                f"quota resume failed for run {run.uid}: {type(exc).__name__}: {exc}",
                extra={"tag": "quota"},
            )
            return {"outcome": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
        return {"outcome": outcome}


async def _resume_one(run, *, now: datetime) -> str:
    from config import settings
    from domains.runs.services.lifecycle import redispatch_run
    from domains.runs.services.quota_retry import RetryAction, decide_retry
    from domains.llm_providers.services.llm_provider_service import select_provider

    quota = dict((run.usage or {}).get("quota") or {})
    retry_count = int(quota.get("retry_count") or 0)
    detected_at = _parse_dt(quota.get("detected_at"))
    exhausted_uids = {str(u) for u in (quota.get("exhausted_provider_uids") or []) if u}

    from domains.llm_providers.services.llm_provider_service import repository_org_uid

    fallback = await select_provider(
        org_uid=await repository_org_uid(run.repository_uid), exclude_uids=exhausted_uids
    )
    action = decide_retry(
        now=now,
        detected_at=detected_at,
        retry_count=retry_count,
        retry_minutes=int(settings.OPENSWEEP_QUOTA_RETRY_MINUTES),
        max_retries=int(settings.OPENSWEEP_QUOTA_MAX_RETRIES),
        fallback_available=fallback is not None,
    )
    if action == RetryAction.WAIT:
        return "waiting"
    if action == RetryAction.EXHAUSTED:
        await _fail_exhausted(run, now=now, retry_count=retry_count)
        return "exhausted"

    # Fallback available → run on it now (exclude the exhausted providers).
    # No fallback but reset window passed → the paused provider's quota has
    # reset; retry it WITHOUT exclusions.
    exclude = exhausted_uids if fallback is not None else frozenset()
    await redispatch_run(run, exclude_provider_uids=exclude)
    return "resumed"


async def _fail_exhausted(run, *, now: datetime, retry_count: int) -> None:
    from domains.execution.services.sandbox_service import SandboxService
    from domains.runs.schemas import RunStatus
    from domains.runs.services import playbooks as playbook_registry
    from infrastructure.audit import write_audit

    run.status = RunStatus.FAILED.value
    run.error = "quota retries exhausted"
    run.completed_at = now
    if run.started_at and not run.duration_ms:
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    run.updated_at = now
    await run.save()
    await write_audit(
        kind="run.failed",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid="quota_resume",
        payload={"error": run.error, "retry_count": retry_count},
    )
    # Discovery sandboxes are lifecycle-managed — nothing left to retry, so
    # destroy. Write sandboxes stay retained for inspection (same as any
    # failed write run).
    if (run.execution_mode or "analyze_only") == "analyze_only":
        sandbox_uid = (run.usage or {}).get("sandbox_uid", "")
        if sandbox_uid:
            try:
                await SandboxService().destroy(sandbox_uid, actor_uid="quota_resume")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"sandbox {sandbox_uid} cleanup failed for exhausted run {run.uid}: {exc}",
                    extra={"tag": "quota"},
                )
    # Fire the playbook completion hook so linked entities never wedge on an
    # exhausted run (e.g. a verify verdict stuck at "pending"). The hooks
    # tolerate non-awaiting_input statuses and never raise.
    await playbook_registry.on_turn_complete(run)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
