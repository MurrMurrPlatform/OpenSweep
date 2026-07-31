"""Celery beat tick — freshness reconcile for every connected repository.

Push webhooks are the realtime path for marking Docs and Areas stale; this
sweep is the correctness backstop. It compares each repository's persisted
freshness cursor against the live default-branch head and replays the gap, so
a dropped delivery, a repo registered after its commits landed, or a rotated
webhook secret self-heals instead of leaving the whole board reading "fresh"
forever with nothing to notice it.

Deterministic and LLM-free (pure path matching over a compare range), so it is
safe to run unconditionally.
"""

from __future__ import annotations

from celery_app import app
from logging_config import logger


@app.task(name="opensweep.repositories.reconcile_freshness")
def reconcile_freshness() -> dict:
    from domains.repositories.services.freshness_sync import reconcile_all_repositories
    from infrastructure.celery_async import run_async_task
    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()

    out = run_async_task(reconcile_all_repositories)
    logger.info(f"freshness reconcile sweep: {out}", extra={"tag": "freshness"})
    return out
