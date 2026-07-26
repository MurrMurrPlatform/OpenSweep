"""Celery beat tick — dispatch implement runs for approved ticket epics."""

from __future__ import annotations

from celery_app import app
from logging_config import logger


@app.task(name="opensweep.tickets.epic_tick")
def epic_tick() -> dict:
    from domains.tickets.services.epic_dispatch import tick_epics
    from infrastructure.celery_async import run_async_task
    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()

    async def _go():
        # run_async_task reconnected the async neomodel driver to this
        # task's fresh event loop (infrastructure/celery_async.py).
        return await tick_epics()

    out = run_async_task(_go)
    logger.info(f"epic tick: {out}", extra={"tag": "tickets"})
    return out
