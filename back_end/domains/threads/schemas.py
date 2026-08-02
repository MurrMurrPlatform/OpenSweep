"""Thread DTOs (unified dev flow)."""

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadDTO(BaseModel):
    uid: str
    repository_uid: str
    subject_ticket_uid: str
    phase: str
    plan_state: str
    branch: str = ""
    pr_uid: str = ""
    ready_for_review: bool = False
    active_run_uid: str = ""
    # Open questions blocking the agent — on the summary DTO (not just the
    # detail progress) so list surfaces (the board) can badge "waiting on you"
    # without fetching every thread's detail.
    questions_open: int = 0
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ThreadRunSummaryDTO(BaseModel):
    uid: str
    playbook: str
    status: str
    title: str = ""
    created_at: datetime | None = None


class ThreadDetailDTO(ThreadDTO):
    plan_text: str = ""
    # Derived at read time from platform-observed facts (questions, plan,
    # PR, verdicts, fix rounds) — never stored, so it cannot drift.
    progress: dict = {}
    events: list[dict] = []
    runs: list[ThreadRunSummaryDTO] = []


class CreateThreadRequest(BaseModel):
    ticket_uid: str = Field(min_length=1)
    # interrogate | assume | strict. "" = inherit (ticket → repository →
    # interrogate). See domains/runs/services/autonomy.py.
    autonomy: str = ""


class UpdateThreadPlanRequest(BaseModel):
    plan_text: str = Field(min_length=1)
