"""list_comments_for must resolve author display names in a single batched
User.nodes.filter — not one lookup per comment. Also verifies OpenSweep- and
local-user-authored rows skip the DB entirely."""

from types import SimpleNamespace

import pytest

from domains.comments import service as svc
from domains.comments.models import Comment
from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

pytestmark = pytest.mark.asyncio


class _User(SimpleNamespace):
    def __init__(self, uid, display_name):
        super().__init__(uid=uid, display_name=display_name)


@pytest.fixture
def env(monkeypatch):
    users = {
        "u1": _User("u1", "Alice"),
        "u2": _User("u2", "Bob"),
    }
    calls: list[dict] = []

    class _Nodes:
        @staticmethod
        async def filter(**kw):
            calls.append(kw)
            uids = kw.get("uid__in") or []
            return [users[u] for u in uids if u in users]

    monkeypatch.setattr(svc, "User", SimpleNamespace(nodes=_Nodes))

    local = SimpleNamespace(uid="local", display_name="Local")

    monkeypatch.setattr(svc, "get_local_user", lambda: local)
    return SimpleNamespace(calls=calls, users=users, local=local)


def _c(uid, author_uid, kind=CommentAuthorKind.USER.value):
    return Comment(
        uid=uid,
        subject_type=CommentSubjectType.TICKET.value,
        subject_uid="t1",
        author_uid=author_uid,
        author_kind=kind,
        body="hi",
    )


async def test_author_names_are_batched(env, monkeypatch):
    comments = [
        _c("c1", "u1"),
        _c("c2", "u2"),
        _c("c3", "u1"),  # duplicate — still one batch call
        _c("c4", "u1", kind=CommentAuthorKind.OPENSWEEP.value),  # no DB call
        _c("c5", env.local.uid),  # local user, no DB call
    ]

    async def fake_filter_comments(**kw):
        return comments

    monkeypatch.setattr(
        svc, "Comment",
        SimpleNamespace(nodes=SimpleNamespace(filter=fake_filter_comments)),
    )

    dtos = await svc.list_comments_for(CommentSubjectType.TICKET, "t1")
    assert [d.author_name for d in dtos] == [
        "Alice", "Bob", "Alice", svc.OPENSWEEP_AUTHOR_NAME, "Local",
    ]
    # Exactly ONE User.nodes.filter call, resolving both real user uids.
    assert len(env.calls) == 1
    assert set(env.calls[0]["uid__in"]) == {"u1", "u2"}


async def test_missing_user_falls_back_to_uid(env, monkeypatch):
    comments = [_c("c1", "ghost")]

    async def fake_filter_comments(**kw):
        return comments

    monkeypatch.setattr(
        svc, "Comment",
        SimpleNamespace(nodes=SimpleNamespace(filter=fake_filter_comments)),
    )

    dtos = await svc.list_comments_for(CommentSubjectType.TICKET, "t1")
    assert dtos[0].author_name == "ghost"


async def test_empty_thread_makes_no_user_lookup(env, monkeypatch):
    async def fake_filter_comments(**kw):
        return []

    monkeypatch.setattr(
        svc, "Comment",
        SimpleNamespace(nodes=SimpleNamespace(filter=fake_filter_comments)),
    )

    dtos = await svc.list_comments_for(CommentSubjectType.TICKET, "t1")
    assert dtos == []
    assert env.calls == []
