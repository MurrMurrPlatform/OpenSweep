"""Celery app for OpenSweep -- broker on Redis DB 0.

PLATFORM.md §Principles: automatic LLM Runs are opt-in per ScheduledAgent. The
schedule tick only dispatches ScheduledAgents whose user-set cron trigger is
due; Doc freshness is driven by GitHub push webhooks, not a beat tick.
"""

import ssl

from celery import Celery
from celery.signals import worker_process_init, worker_ready

from redis_config import get_redis_url

broker_url = get_redis_url(db=0)
result_backend = get_redis_url(db=0)

broker_use_ssl = None
redis_backend_use_ssl = None
if broker_url.startswith("rediss://"):
    broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE}
    redis_backend_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE}

app = Celery(
    "opensweep",
    broker=broker_url,
    backend=result_backend,
    include=[
        "domains.execution.tasks.cleanup_sandboxes",
        "domains.agents.tasks.schedule_tick",
        "domains.runs.tasks.resume_paused",
        "domains.runs.tasks.reconcile_runs",
        "domains.delivery.tasks.sync_pull_requests",
        "domains.slack.tasks.deliver",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_time_limit=900,
    task_soft_time_limit=600,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_use_ssl=broker_use_ssl,
    redis_backend_use_ssl=redis_backend_use_ssl,
    # Only deterministic, no-LLM ticks are scheduled. LLM Runs remain
    # opt-in (Sweep button or Ask form). Doc freshness is webhook-driven:
    # GitHub push events feed changed paths into
    # domains/docs/services/doc_freshness.mark_docs_stale.
    beat_schedule={
        "agent-schedule-tick": {
            "task": "opensweep.agents.schedule_tick",
            "schedule": 60.0,  # every minute — finest cron resolution
        },
        # Quota pause/resume (PLATFORM_V2_DESIGN.md §8): the beat task only
        # SELECTS eligible paused runs and enqueues one
        # opensweep.runs.resume_run task per run — the actual
        # re-dispatch (a full CLI run) happens in that per-run task, which
        # carries its own 3600/3900s limits instead of the global 600/900s.
        "run-resume-paused": {
            "task": "opensweep.runs.resume_paused_runs",
            "schedule": 600.0,  # every 10 minutes
        },
        # Destroy sandboxes whose cleanup_after has passed. The task existed
        # but was never scheduled — expired sandboxes accumulated forever.
        "sandbox-cleanup": {
            "task": "opensweep.execution.cleanup_sandboxes",
            "schedule": 1800.0,  # every 30 minutes
        },
        # Fail runs whose dispatching process died (dispatch is an in-process
        # asyncio task — a crash strands the row in queued/running). Liveness
        # is transcript-stream mtime, so this also covers local providers
        # that have no wall ceiling.
        "run-reconcile": {
            "task": "opensweep.runs.reconcile_stale_runs",
            "schedule": 300.0,  # every 5 minutes
        },
        # 2-way PR reconcile: webhooks are the realtime path; this sweep
        # imports PRs opened outside OpenSweep and closes out externally
        # merged/closed ones. Deterministic GitHub reads, no LLM.
        "pull-request-sync": {
            "task": "opensweep.delivery.sync_pull_requests",
            "schedule": 300.0,  # every 5 minutes
        },
    },
)


@worker_process_init.connect
def init_worker(**_kwargs):
    # Imports stay inside the function — module-level config imports are
    # consumed by migration_tool.
    import sys

    from config import settings
    from infrastructure.neomodel_config import configure_neomodel
    from infrastructure.process_role import WORKER, set_role
    from infrastructure.production_guards import enforce_production_guards
    from logging_config import logger

    try:
        enforce_production_guards(settings)
    except RuntimeError as exc:
        logger.critical(f"production configuration invalid — worker refusing to start:\n{exc}")
        sys.exit(1)

    configure_neomodel()
    # Runs dispatched from this process (schedule ticks, quota resumes) are
    # stamped usage["dispatch_runtime"]="worker" so the worker_ready sweep
    # below can fail exactly its own orphans after a restart.
    set_role(WORKER)


@worker_ready.connect
def sweep_worker_orphans(**_kwargs):
    """A worker restart killed any dispatch task it was running — fail the
    runs stamped as worker-owned now instead of waiting for the liveness
    tick. Best-effort: a failure here must never block worker startup."""
    try:
        from domains.runs.services.run_reconciliation import (
            reconcile_orphaned_runs,
        )
        from infrastructure.celery_async import run_async_task
        from infrastructure.neomodel_config import configure_neomodel
        from infrastructure.process_role import WORKER
        from logging_config import logger

        configure_neomodel()

        async def _go() -> int:
            return await reconcile_orphaned_runs(role=WORKER)

        changed = run_async_task(_go)
        if changed:
            logger.info(f"failed {changed} orphaned worker run(s) after restart")
    except Exception as exc:  # noqa: BLE001
        from logging_config import logger

        logger.warning(f"worker orphaned-run sweep skipped: {exc}")
