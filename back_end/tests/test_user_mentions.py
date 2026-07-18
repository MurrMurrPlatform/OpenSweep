"""`@[Name](user:uid)` mentions — parsing accepts the new `user` type, and
create_comment records one `comment.mention` audit event per mentioned user
(never the author), anchored to the subject's repository. DB-free."""

from types import SimpleNamespace

import pytest

from domains.comments import mentions as mention_lib
from domains.comments import service as comment_service
from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

pytestmark = pytest.mark.asyncio


# ── parsing ──────────────────────────────────────────────────────────────────


def test_user_mentions_parse():
    refs = mention_lib.parse_item_mentions(
        "@[Alice](user:abc123) please look at @[Fix login](ticket:t1)"
    )
    assert refs == [
        {"type": "user", "uid": "abc123", "label": "Alice"},
        {"type": "ticket", "uid": "t1", "label": "Fix login"},
    ]


def test_user_mentions_helper_selects_only_users():
    refs = [
        {"type": "user", "uid": "abc123", "label": "Alice"},
        {"type": "ticket", "uid": "t1", "label": "Fix login"},
        {"type": "user", "uid": "", "label": "broken"},
    ]
    assert mention_lib.user_mentions(refs) == [
        {"type": "user", "uid": "abc123", "label": "Alice"}
    ]


# ── create_comment → comment.mention audits ──────────────────────────────────


class _FakeComment(SimpleNamespace):
    async def save(self):
        return self


@pytest.fixture
def audit_log(monkeypatch):
    written: list[dict] = []

    async def fake_write_audit(**kwargs):
        written.append(kwargs)

    async def fake_subject_repository_uid(subject_type, subject_uid):
        return "repo-1"

    monkeypatch.setattr(comment_service, "Comment", _FakeComment)
    monkeypatch.setattr(comment_service, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        comment_service, "subject_repository_uid", fake_subject_repository_uid
    )
    return written


async def test_user_mention_writes_comment_mention_audit(audit_log):
    await comment_service.create_comment(
        subject_type=CommentSubjectType.TICKET,
        subject_uid="t1",
        body="@[Alice](user:alice1) can you take this? cc @[Bob](user:bob2)",
        author_uid="author1",
        author_kind=CommentAuthorKind.USER,
    )
    kinds = [w["kind"] for w in audit_log]
    assert kinds == ["comment.created", "comment.mention", "comment.mention"]
    created = audit_log[0]
    assert created["repository_uid"] == "repo-1"  # org-visible, not platform-level
    mentions = audit_log[1:]
    assert {m["payload"]["mentioned_user_uid"] for m in mentions} == {"alice1", "bob2"}
    for m in mentions:
        assert m["repository_uid"] == "repo-1"
        assert m["payload"]["comment_subject_type"] == "ticket"
        assert m["payload"]["comment_subject_uid"] == "t1"


async def test_self_mention_is_not_notified(audit_log):
    await comment_service.create_comment(
        subject_type=CommentSubjectType.FINDING,
        subject_uid="f1",
        body="note to self @[Me](user:author1)",
        author_uid="author1",
    )
    assert [w["kind"] for w in audit_log] == ["comment.created"]


async def test_plain_comment_writes_no_mention_audit(audit_log):
    await comment_service.create_comment(
        subject_type=CommentSubjectType.FINDING,
        subject_uid="f1",
        body="just a note about @[Fix login](ticket:t1)",
        author_uid="author1",
    )
    assert [w["kind"] for w in audit_log] == ["comment.created"]
