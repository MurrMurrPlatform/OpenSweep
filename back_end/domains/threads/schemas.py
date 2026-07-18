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
    active_run_uid: str = ""
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
    # Native executor todos mirrored per phase: {phase: [{content, status,
    # activeForm}]}.
    todos: dict = {}
    events: list[dict] = []
    runs: list[ThreadRunSummaryDTO] = []


class CreateThreadRequest(BaseModel):
    ticket_uid: str = Field(min_length=1)


class UpdateThreadPlanRequest(BaseModel):
    plan_text: str = Field(min_length=1)
