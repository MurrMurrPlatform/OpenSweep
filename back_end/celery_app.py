"""Celery app for OpenSweep -- broker on Redis DB 0.

PLATFORM.md §Principles: automatic LLM Runs are opt-in per ScheduledAgent. The
schedule tick only dispatches ScheduledAgents whose user-set cron trigger is
due. Doc/Area freshness is driven by GitHub push webhooks in realtime; the
freshness-reconcile tick is a deterministic backstop that replays deliveries
the webhook never landed (it marks stale, it never starts a Run).
"""

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown, worker_ready

from redis_config import get_redis_ssl_options, get_redis_url

broker_url = get_redis_url(db=0)
result_backend = get_redis_url(db=0)

# Certificate verification is REQUIRED (redis_config.get_redis_ssl_options) —
# the broker carries run-dispatch payloads and the Redis AUTH password, so an
# unverified handshake would hand both to anyone on the path. A private CA
# goes in REDIS_CA_CERTS; disabling the check is never the answer.
_ssl_options = get_redis_ssl_options() if broker_url.startswith("rediss://") else None
broker_use_ssl = dict(_ssl_options) if _ssl_options else None
redis_backend_use_ssl = dict(_ssl_options) if _ssl_options else None

app = Celery(
    "opensweep",
    broker=broker_url,
    backend=result_backend,
    include=[
        "domains.execution.tasks.cleanup_sandboxes",
        "domains.agents.tasks.schedule_tick",
        "domains.campaigns.tasks.campaign_tick",
        "domains.runs.tasks.resume_paused",
        "domains.runs.tasks.reconcile_runs",
        "domains.runs.tasks.dispatch_runs",
        "domains.delivery.tasks.sync_pull_requests",
        "domains.repositories.tasks.reconcile_freshness",
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
    # With task_acks_late, the Redis broker redelivers any task still
    # in-flight after visibility_timeout (default 3600s) — which would spawn a
    # duplicate of every run longer than an hour. Per-run tasks set their own
    # Celery limit up to MAX_DISPATCH_SECONDS + grace (86400 + 300 = 86700s;
    # domains/runs/tasks/task_limits.py), so the timeout must sit above that.
    broker_transport_options={"visibility_timeout": 90000},
    broker_connection_retry_on_startup=True,
    broker_use_ssl=broker_use_ssl,
    redis_backend_use_ssl=redis_backend_use_ssl,
    # Only deterministic, no-LLM ticks are scheduled. LLM Runs remain
    # opt-in (Sweep button or Ask form). Freshness is webhook-driven — GitHub
    # push events to the default branch feed changed paths into
    # domains/repositories/services/freshness_sync — with freshness-reconcile
    # below as the missed-delivery backstop.
    beat_schedule={
        "agent-schedule-tick": {
            "task": "opensweep.agents.schedule_tick",
            "schedule": 60.0,  # every minute — finest cron resolution
        },
        # Advance running audit campaigns: mark finished parts, dispatch the
        # next pending ones up to max_parallel, finalize completed ones. Only
        # campaigns a user (or a due scheduled binding) explicitly launched
        # are ever ticked forward — the tick itself starts nothing new.
        "campaign-tick": {
            "task": "opensweep.campaigns.tick",
            "schedule": 60.0,  # every minute — parts chain promptly
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
        # Freshness backstop: push webhooks mark Docs/Areas stale in realtime,
        # but a dropped delivery would otherwise leave the board reading
        # "fresh" forever. This replays the gap between each repo's freshness
        # cursor and its live default-branch head. Deterministic, no LLM.
        "freshness-reconcile": {
            "task": "opensweep.repositories.reconcile_freshness",
            "schedule": 900.0,  # every 15 minutes
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
