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
    SCORE_DIMENSIONS,
)
from domains.analysis.services.analysis_service import (
    analysis_write_lock,
    get_or_create_analysis,
)
from infrastructure.audit import write_audit


def _validated_scorecard(scorecard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject scorecard entries the read path cannot hydrate.

    `dimension` is required on ScorecardEntryDTO, so an entry without it used
    to be stored happily and then blow up analysis_to_dto on EVERY subsequent
    read of that Analysis — a write that permanently breaks reads, with no API
    route able to repair it. Fail the write loudly instead; the agent gets a
    422 it can act on rather than a report that 500s later.
    """
    validated: list[dict[str, Any]] = []
    for idx, entry in enumerate(scorecard):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail=f"scorecard[{idx}] must be an object, got {type(entry).__name__}",
            )
        dimension = str(entry.get("dimension") or "").strip()
        if not dimension:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"scorecard[{idx}] is missing required 'dimension'; each entry is "
                    "{dimension, score, max, grade, rationale} — suggested dimensions: "
                    f"{', '.join(SCORE_DIMENSIONS)}"
                ),
            )
        grade = str(entry.get("grade") or "").strip()
        if grade and grade not in HEALTH_GRADES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"scorecard[{idx}] has invalid grade={grade!r}; "
                    f"expected one of {sorted(HEALTH_GRADES)}"
                ),
            )
        validated.append({**entry, "dimension": dimension})
    return validated


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
    if scorecard is not None:
        scorecard = _validated_scorecard(scorecard)

    # Held across get_or_create → mutate → save so a concurrent
    # set_analysis_section / add_analysis_note / ask_question call for the
    # same source_run_uid cannot read the same base state and silently drop
    # this write's fields on its own save. See analysis_write_lock_key.
    async with analysis_write_lock(source_run_uid):
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
            node.scorecard = scorecard  # already validated above
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
