"""Analysis service — read + finding-join for the deep-scan report.

Authoring (create/append) happens through the platform tools (Phase B); this
service owns the read surface (list/get/latest) and the DTO conversion,
including the Finding roll-up joined by source_run_uid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.analysis.models import Analysis
from domains.analysis.schemas import (
    AnalysisDTO,
    AnalysisQuestionDTO,
    AnalysisStatus,
    CoverageEntryDTO,
    QuestionStatus,
    ScorecardEntryDTO,
    StrengthDTO,
    ValidationEntryDTO,
)
from domains.findings.models import Finding


async def get_or_create_analysis(
    *,
    repository_uid: str,
    source_run_uid: str,
    executor: str = "",
    revision: str = "",
) -> Analysis:
    """Fetch the single Analysis for a run, creating it on first authoring call.

    Any of the four authoring tools may be the first to run, so each lazily
    creates the shell. The unique constraint on source_run_uid makes a
    concurrent double-create fail on save — we re-fetch on that race.
    """
    existing = await Analysis.nodes.get_or_none(source_run_uid=source_run_uid)
    if existing is not None:
        return existing
    node = Analysis(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        source_run_uid=source_run_uid,
        executor=executor or "",
        revision=revision or "",
        status="in_progress",
    )
    try:
        await node.save()
    except Exception:  # noqa: BLE001 — uniqueness race: another call created it
        raced = await Analysis.nodes.get_or_none(source_run_uid=source_run_uid)
        if raced is None:
            raise
        return raced
    return node


async def finalize_analysis_for_run(run_uid: str) -> bool:
    """Flip an in-progress Analysis to `complete` when its run's turn ends.

    Idempotent and best-effort: the deep-scan agent usually sets status itself
    via upsert_analysis, but a killed or forgetful run still leaves a
    finalized report. Returns True if a flip happened."""
    node = await Analysis.nodes.get_or_none(source_run_uid=run_uid)
    if node is None or (node.status or "") != "in_progress":
        return False
    node.status = "complete"
    if not node.completed_at:
        node.completed_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    await node.save()
    return True


def analysis_to_dto(a: Analysis) -> AnalysisDTO:
    questions = [_question_dto(q) for q in (a.questions or [])]
    return AnalysisDTO(
        uid=a.uid,
        repository_uid=a.repository_uid,
        source_run_uid=a.source_run_uid,
        revision=a.revision or "",
        title=a.title or "",
        status=AnalysisStatus(a.status or "in_progress"),
        supersedes=a.supersedes or "",
        superseded_by=a.superseded_by or "",
        executor=a.executor or "",
        health_grade=a.health_grade or "",
        health_score=a.health_score,
        scorecard=[ScorecardEntryDTO(**e) for e in _dicts(a.scorecard)],
        confidence=a.confidence or "",
        limitations=a.limitations or "",
        stats=dict(a.stats or {}),
        sections=dict(a.sections or {}),
        coverage=[CoverageEntryDTO(**e) for e in _dicts(a.coverage)],
        strengths=[StrengthDTO(**e) for e in _dicts(a.strengths)],
        validation_baseline=[ValidationEntryDTO(**e) for e in _dicts(a.validation_baseline)],
        questions=questions,
        open_question_count=sum(1 for q in questions if q.status == QuestionStatus.OPEN),
        created_at=a.created_at,
        updated_at=a.updated_at,
        completed_at=a.completed_at,
    )


def _dicts(value) -> list[dict]:
    """Coerce a JSON list to a list of dicts, dropping malformed entries so a
    single bad agent-authored item never breaks the whole DTO."""
    return [e for e in (value or []) if isinstance(e, dict)]


def _question_dto(q: dict) -> AnalysisQuestionDTO:
    return AnalysisQuestionDTO(
        uid=str(q.get("uid") or ""),
        question=str(q.get("question") or ""),
        why_it_matters=str(q.get("why_it_matters") or ""),
        category=str(q.get("category") or ""),
        status=QuestionStatus(q.get("status") or "open"),
        answer=str(q.get("answer") or ""),
        answered_by=str(q.get("answered_by") or ""),
        answered_at=q.get("answered_at"),
    )


async def _attach_finding_rollup(dto: AnalysisDTO) -> AnalysisDTO:
    """Fill finding_count + findings_by_severity from Findings sharing the
    Analysis's source_run_uid (the free join)."""
    by_sev: dict[str, int] = {}
    count = 0
    for f in await Finding.nodes.all():
        if f.source_run_uid != dto.source_run_uid:
            continue
        count += 1
        sev = f.severity or "medium"
        by_sev[sev] = by_sev.get(sev, 0) + 1
    dto.finding_count = count
    dto.findings_by_severity = by_sev
    return dto


class AnalysisService:
    async def list(
        self,
        *,
        repository_uid: str | None = None,
        status: str | None = None,
        include_superseded: bool = True,
    ) -> list[AnalysisDTO]:
        nodes = await Analysis.nodes.all()
        out: list[AnalysisDTO] = []
        for a in nodes:
            if repository_uid and a.repository_uid != repository_uid:
                continue
            if status and (a.status or "") != status:
                continue
            if not include_superseded and (a.status or "") == "superseded":
                continue
            out.append(analysis_to_dto(a))
        out.sort(
            key=lambda d: d.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return out

    async def get(self, uid: str) -> AnalysisDTO:
        a = await Analysis.nodes.get_or_none(uid=uid)
        if a is None:
            raise HTTPException(status_code=404, detail=f"Analysis {uid} not found")
        return await _attach_finding_rollup(analysis_to_dto(a))

    async def get_node(self, uid: str) -> Analysis:
        a = await Analysis.nodes.get_or_none(uid=uid)
        if a is None:
            raise HTTPException(status_code=404, detail=f"Analysis {uid} not found")
        return a

    async def answer_question(
        self, uid: str, qid: str, *, answer: str, actor: str = ""
    ) -> AnalysisDTO:
        node = await self.get_node(uid)
        questions = list(node.questions or [])
        for q in questions:
            if q.get("uid") == qid:
                q["answer"] = answer
                q["status"] = "answered"
                q["answered_by"] = actor
                q["answered_at"] = datetime.now(UTC).isoformat()
                break
        else:
            raise HTTPException(status_code=404, detail=f"question {qid} not found")
        node.questions = questions
        node.updated_at = datetime.now(UTC)
        await node.save()
        return await _attach_finding_rollup(analysis_to_dto(node))

    async def dismiss_question(self, uid: str, qid: str) -> AnalysisDTO:
        node = await self.get_node(uid)
        questions = list(node.questions or [])
        for q in questions:
            if q.get("uid") == qid:
                q["status"] = "dismissed"
                break
        else:
            raise HTTPException(status_code=404, detail=f"question {qid} not found")
        node.questions = questions
        node.updated_at = datetime.now(UTC)
        await node.save()
        return await _attach_finding_rollup(analysis_to_dto(node))

    async def refine_with_answers(self, uid: str, *, triggered_by: str = "") -> dict:
        """Dispatch a fresh deep-scan that ingests the answered questions, and
        supersede this Analysis with the one the new run will author."""
        old = await self.get_node(uid)
        answered = [
            q
            for q in (old.questions or [])
            if q.get("status") == "answered" and (q.get("answer") or "").strip()
        ]
        if not answered:
            raise HTTPException(
                status_code=422,
                detail="no answered questions to refine with — answer at least one first",
            )
        qa = "\n".join(
            f"- Q: {q.get('question', '')}\n  A: {q.get('answer', '')}" for q in answered
        )
        focus = (
            "The user answered previously-open questions from an earlier scan of "
            "this repository. Treat these answers as ground truth — they resolve "
            "unknowns and should sharpen your findings, scorecard, and "
            "recommendations:\n\n" + qa
        )

        # Lazy imports: sweep/effort pull in the run-dispatch stack.
        from domains.runs.schemas import Effort
        from domains.runs.services.sweep import run_deep_scan
        from domains.run_policies.services.effort import ensure_policy_for_effort

        policy = await ensure_policy_for_effort(Effort.DEEP)
        result = await run_deep_scan(
            repository_uid=old.repository_uid,
            triggered_by=triggered_by or "refine",
            custom_intent=focus,
            run_policy_uid=policy.uid,
        )
        if not result.run_uid:
            raise HTTPException(
                status_code=502,
                detail=f"refine dispatch failed: {'; '.join(result.errors) or 'unknown error'}",
            )

        # Pre-create the superseding Analysis shell so the link exists before
        # the agent authors it (its upsert_analysis will find this node).
        new = await get_or_create_analysis(
            repository_uid=old.repository_uid,
            source_run_uid=result.run_uid,
        )
        new.supersedes = old.uid
        new.title = old.title or "Deep scan — whole repository"
        new.updated_at = datetime.now(UTC)
        await new.save()

        old.status = "superseded"
        old.superseded_by = new.uid
        old.updated_at = datetime.now(UTC)
        await old.save()
        return {
            "analysis_uid": new.uid,
            "run_uid": result.run_uid,
            "supersedes": old.uid,
        }

    async def latest_for_repo(self, repository_uid: str) -> AnalysisDTO | None:
        """Newest non-superseded Analysis for the repo — what Health shows."""
        candidates = await self.list(
            repository_uid=repository_uid, include_superseded=False
        )
        if not candidates:
            return None
        # get() adds the finding roll-up; list() intentionally omits it.
        return await self.get(candidates[0].uid)
