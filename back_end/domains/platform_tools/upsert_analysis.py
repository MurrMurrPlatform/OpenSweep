"""Platform tool: upsert_analysis.

Create-or-update the deep-scan Analysis for a run (keyed by source_run_uid).
Sets the verdict layer — title, status, health grade/score, scorecard,
confidence, limitations, stats. Only provided (non-None) fields are written,
so the agent can call it repeatedly as the picture sharpens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from fastapi import HTTPException

from domains.analysis.models import (
    ANALYSIS_STATUSES,
    CONFIDENCE_LABELS,
    HEALTH_GRADES,
)
from domains.analysis.services.analysis_service import get_or_create_analysis
from infrastructure.audit import write_audit


async def upsert_analysis(
    *,
    repository_uid: str,
    source_run_uid: str,
    title: Optional[str] = None,
    status: Optional[str] = None,
    revision: Optional[str] = None,
    health_grade: Optional[str] = None,
    health_score: Optional[int] = None,
    scorecard: Optional[list[dict[str, Any]]] = None,
    confidence: Optional[str] = None,
    limitations: Optional[str] = None,
    stats: Optional[dict[str, Any]] = None,
    executor: str = "",
) -> dict[str, Any]:
    if status is not None and status not in ANALYSIS_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid status={status!r}; expected one of {sorted(ANALYSIS_STATUSES)}",
        )
    if health_grade:
        if health_grade not in HEALTH_GRADES:
            raise HTTPException(
                status_code=422,
                detail=f"invalid health_grade={health_grade!r}; expected one of {sorted(HEALTH_GRADES)}",
            )
    if confidence and confidence not in CONFIDENCE_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid confidence={confidence!r}; expected one of {sorted(CONFIDENCE_LABELS)}",
        )

    node = await get_or_create_analysis(
        repository_uid=repository_uid,
        source_run_uid=source_run_uid,
        executor=executor,
        revision=revision or "",
    )

    if title is not None:
        node.title = title
    if status is not None:
        node.status = status
        if status == "complete" and not node.completed_at:
            node.completed_at = datetime.now(UTC)
    if revision is not None:
        node.revision = revision
    if health_grade is not None:
        node.health_grade = health_grade
    if health_score is not None:
        node.health_score = int(health_score)
    if scorecard is not None:
        node.scorecard = [e for e in scorecard if isinstance(e, dict)]
    if confidence is not None:
        node.confidence = confidence
    if limitations is not None:
        node.limitations = limitations
    if stats is not None:
        node.stats = {**(node.stats or {}), **stats}
    if executor and not (node.executor or ""):
        node.executor = executor
    node.updated_at = datetime.now(UTC)
    await node.save()

    await write_audit(
        kind="analysis.upserted",
        subject_uid=node.uid,
        subject_type="Analysis",
        actor_uid=executor or "agent",
        payload={"source_run_uid": source_run_uid, "status": node.status},
    )
    return {"analysis_uid": node.uid, "status": node.status}
