"""Comment DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CommentSubjectType(StrEnum):
    FINDING = "finding"
    TICKET = "ticket"
    PULL_REQUEST = "pull_request"
    NEWS_ITEM = "news_item"
    RUN = "run"
    SCHEDULED_AGENT = "scheduled_agent"
    DOC = "doc"


class CommentAuthorKind(StrEnum):
    USER = "user"
    OPENSWEEP = "opensweep"


class MentionRef(BaseModel):
    """One `@[Label](type:uid)` token parsed out of a comment body."""

    type: str
    uid: str
    label: str = ""


class CommentDTO(BaseModel):
    uid: str
    subject_type: CommentSubjectType
    subject_uid: str
    author_uid: str
    author_name: str = ""
    author_kind: CommentAuthorKind = CommentAuthorKind.USER
    source_run_uid: str = ""
    body: str
    mentions: list[MentionRef] = []
    parent_comment_uid: str = ""
    meta: dict = {}
    # Set on the create response when the body summoned @opensweep and a
    # background run was dispatched to answer.
    triggered_run_uid: str = ""
    created_at: datetime | None = None


class CreateCommentRequest(BaseModel):
    subject_type: CommentSubjectType
    subject_uid: str = Field(min_length=1)
    body: str = Field(min_length=1)
    # Reply threading: uid of the comment this replies to ("" = top-level).
    parent_comment_uid: str = ""



class PendingOpenSweepRunDTO(BaseModel):
    """An in-flight @opensweep reply run for a thread — drives the thread's
    thinking bubble across page reloads."""

    run_uid: str
    comment_uid: str = ""
    status: str = ""
    started_at: datetime | None = None


class MentionSearchResult(BaseModel):
    """One row in the @-mention dropdown."""

    type: str
    uid: str
    label: str
    sublabel: str = ""
    repository_uid: str = ""
