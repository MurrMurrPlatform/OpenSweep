"""Analysis DTOs.

The structured sub-collections (scorecard, coverage, strengths, validation,
questions) are agent-authored JSON on the node. The DTOs give the frontend a
typed shape while tolerating partial/missing keys via defaults.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AnalysisStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DISMISSED = "dismissed"


class ScorecardEntryDTO(BaseModel):
    dimension: str
    score: Optional[float] = None
    max: float = 100
    grade: str = ""
    rationale: str = ""


class CoverageEntryDTO(BaseModel):
    area: str = ""
    paths: list[str] = Field(default_factory=list)
    status: str = "examined"  # examined | partial | skipped
    note: str = ""


class StrengthDTO(BaseModel):
    title: str = ""
    detail: str = ""
    paths: list[str] = Field(default_factory=list)


class ValidationEntryDTO(BaseModel):
    check: str = ""
    command: str = ""
    result: str = ""
    details: str = ""


class AnalysisQuestionDTO(BaseModel):
    uid: str
    question: str = ""
    why_it_matters: str = ""
    category: str = ""
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str = ""
    answered_by: str = ""
    answered_at: Optional[datetime] = None


class AnalysisDTO(BaseModel):
    uid: str
    repository_uid: str
    source_run_uid: str
    revision: str = ""

    title: str = ""
    status: AnalysisStatus = AnalysisStatus.IN_PROGRESS
    supersedes: str = ""
    superseded_by: str = ""
    executor: str = ""

    health_grade: str = ""
    health_score: Optional[int] = None
    scorecard: list[ScorecardEntryDTO] = Field(default_factory=list)
    confidence: str = ""
    limitations: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)

    sections: dict[str, str] = Field(default_factory=dict)

    coverage: list[CoverageEntryDTO] = Field(default_factory=list)
    strengths: list[StrengthDTO] = Field(default_factory=list)
    validation_baseline: list[ValidationEntryDTO] = Field(default_factory=list)

    questions: list[AnalysisQuestionDTO] = Field(default_factory=list)

    # Computed at read time from Findings sharing source_run_uid.
    finding_count: int = 0
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    open_question_count: int = 0

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
