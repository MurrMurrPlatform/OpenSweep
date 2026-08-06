"""Platform add-comment DTO — executor-facing shape for @opensweep replies.

`AddCommentRequest` is deliberately smaller than the human-side
`CreateCommentRequest`: agents post a reply on an existing thread, they don't
open a new one; parent-comment threading is applied by the route based on the
run-token headers, not the request. If the guards weaken, an executor could
post an empty body or a comment on nothing.
"""

import pytest
from pydantic import ValidationError

from api.v1.platform_tools_comments import AddCommentRequest
from domains.comments.schemas import CommentSubjectType


def test_valid_request_round_trips():
    req = AddCommentRequest(
        subject_type="ticket",
        subject_uid="t1",
        body="Applied the policy answer here.",
    )
    assert req.subject_type == CommentSubjectType.TICKET
    assert req.subject_uid == "t1"
    assert req.body == "Applied the policy answer here."


def test_body_may_not_be_empty():
    """An empty reply is a bug — either the run had nothing to say (in which
    case it shouldn't have called the tool) or the payload was truncated."""
    with pytest.raises(ValidationError):
        AddCommentRequest(subject_type="ticket", subject_uid="t1", body="")


def test_subject_uid_may_not_be_empty():
    with pytest.raises(ValidationError):
        AddCommentRequest(subject_type="ticket", subject_uid="", body="hi")


def test_subject_type_rejects_unknown_values():
    """A stale executor with a new subject type must not post a comment on
    a non-existent thread — the enum is the contract."""
    with pytest.raises(ValidationError):
        AddCommentRequest(subject_type="repository", subject_uid="r1", body="hi")


def test_every_documented_subject_type_is_accepted():
    for kind in CommentSubjectType:
        req = AddCommentRequest(subject_type=kind.value, subject_uid="x1", body="hi")
        assert req.subject_type == kind


def test_extra_fields_are_ignored_not_reflected():
    """The route pulls parent-comment / run-uid from headers — a rogue
    executor supplying `parent_comment_uid` in the body must not land on
    the DTO."""
    req = AddCommentRequest(
        subject_type="ticket",
        subject_uid="t1",
        body="hi",
        parent_comment_uid="c-fake",  # ignored
        source_run_uid="r-fake",  # ignored
    )
    assert not hasattr(req, "parent_comment_uid")
    assert not hasattr(req, "source_run_uid")
