"""render_mentioned_items — one Cypher call per mentioned kind (not per
mention). Also pins the TicketGroupProposal→EpicProposal rename: the
`group` code path now imports EpicProposal, so mentioning a group in a
briefing no longer raises ImportError."""

import pytest

import domains.comments.service as svc_mod
from domains.comments.schemas import CommentSubjectType

pytestmark = pytest.mark.asyncio


class _Subject:
    def __init__(self, uid, repository_uid, title="item"):
        self.uid = uid
        self.repository_uid = repository_uid
        self.title = title
        self.status = "open"
        self.member_ticket_uids = ["t1", "t2"]


@pytest.fixture(autouse=True)
def batched_subjects(monkeypatch):
    subjects = {
        "f1": _Subject("f1", "repo-a", "Finding 1"),
        "f2": _Subject("f2", "repo-a", "Finding 2"),
        "t1": _Subject("t1", "repo-a", "Ticket 1"),
    }
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_get_subjects(subject_type, uids):
        calls.append((subject_type.value, tuple(sorted(uids))))
        return {u: subjects[u] for u in uids if u in subjects}

    def fake_snapshot(subject_type, subject):
        return f"{subject_type.value} {subject.uid}: {subject.title}"

    monkeypatch.setattr(svc_mod, "get_subjects", fake_get_subjects)
    monkeypatch.setattr(svc_mod, "subject_snapshot", fake_snapshot)
    yield calls


async def test_mentions_of_same_kind_share_one_lookup(batched_subjects):
    refs = [
        {"type": CommentSubjectType.FINDING.value, "uid": "f1", "label": "F1"},
        {"type": CommentSubjectType.FINDING.value, "uid": "f2", "label": "F2"},
        {"type": CommentSubjectType.TICKET.value, "uid": "t1", "label": "T1"},
    ]
    rendered = await svc_mod.render_mentioned_items(refs, {"repo-a"})
    # Two subject kinds → two get_subjects calls, not one per ref.
    assert len(batched_subjects) == 2
    kinds = {call[0] for call in batched_subjects}
    assert kinds == {"finding", "ticket"}
    assert "F" not in rendered or "Finding 1" in rendered
    assert "Ticket 1" in rendered


async def test_group_mention_uses_epic_proposal(monkeypatch, batched_subjects):
    # Fake EpicProposal.nodes.filter so we can exercise the group path DB-free.
    from types import SimpleNamespace

    class _Nodes:
        @staticmethod
        async def filter(**kw):
            uids = kw.get("uid__in") or []
            return [
                _Subject(u, "repo-a", f"Group {u}") for u in uids if u == "g1"
            ]

    fake_epic = SimpleNamespace(nodes=_Nodes)

    # Patch the lazy import target: the service does
    # `from domains.tickets.models import EpicProposal` inside the branch.
    import domains.tickets.models as tm

    monkeypatch.setattr(tm, "EpicProposal", fake_epic)

    refs = [{"type": "group", "uid": "g1", "label": "G1"}]
    rendered = await svc_mod.render_mentioned_items(refs, {"repo-a"})
    assert "group g1" in rendered
    assert "Group g1" in rendered
    # Group path does NOT go through get_subjects.
    assert not any(call[0] == "group" for call in batched_subjects)


async def test_empty_scope_still_fails_closed(batched_subjects):
    refs = [{"type": CommentSubjectType.FINDING.value, "uid": "f1", "label": "F"}]
    rendered = await svc_mod.render_mentioned_items(refs, set())
    assert rendered == ""


async def test_unknown_kind_is_ignored(batched_subjects):
    refs = [{"type": "does-not-exist", "uid": "x1", "label": "X"}]
    rendered = await svc_mod.render_mentioned_items(refs, {"repo-a"})
    assert rendered == ""
    assert batched_subjects == []
