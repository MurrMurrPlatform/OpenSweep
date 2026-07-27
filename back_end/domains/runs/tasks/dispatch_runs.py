"""Celery task — execute a QUEUED run's dispatch pipeline in its own task.

Run dispatch (clone → RUNNING → agent turn) can take an hour for CLI runs, far
beyond the 600/900s limits on the beat ticks (campaign tick, auto-audit,
schedule tick) that create the queued row. Those ticks therefore hand off to
this per-run task instead of running the pipeline inline: an in-loop asyncio
task would be cancelled the instant the tick's `asyncio.run` loop closes,
leaving the run stuck QUEUED. Mirrors `resume_run` (tasks/resume_paused.py) —
per-run task, extended time limits, its own event loop for the whole run.

The limits below are only the FALLBACK for a task enqueued without explicit
ones. `_launch_dispatch` derives per-invocation limits from the run's policy
(`tasks/task_limits.py`) and passes them to `apply_async`, so a `deep` run
gets a limit above its own 14400s ceiling instead of being killed at 3900.
"""

from __future__ import annotations

from celery_app import app
from domains.runs.tasks.task_limits import (
    DEFAULT_DISPATCH_SECONDS,
    dispatch_time_limits,
)
from logging_config import logger

_FALLBACK_SOFT, _FALLBACK_HARD = dispatch_time_limits(DEFAULT_DISPATCH_SECONDS)


@app.task(
    name="opensweep.runs.dispatch_run",
    soft_time_limit=_FALLBACK_SOFT,
    time_limit=_FALLBACK_HARD,
)
def dispatch_run(run_uid: str) -> dict:
    """Dispatch ONE queued run — may execute a full CLI run, hence the per-task
    limits overriding the global 600/900s."""
    from infrastructure.celery_async import run_async_task
    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()

    async def _go() -> dict:
        from domains.runs.services.lifecycle import execute_queued_run

        return await execute_queued_run(run_uid)

    out = run_async_task(_go)
    logger.info(f"dispatch run {run_uid}: {out}", extra={"tag": "lifecycle"})
    return out
