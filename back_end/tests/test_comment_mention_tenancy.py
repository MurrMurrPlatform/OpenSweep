"""F2 (HIGH) — comment @-mentions must not resolve cross-org data.

WHY: a comment body can carry `@[Label](type:uid)` item-mentions. They are
parsed and stored unvalidated, and when the body also says `@opensweep`,
`render_mentioned_items` resolves EACH mentioned uid to a full snapshot
(title, description, severity, status, …) that is injected into the summoned
run's prompt — and echoed back into the thread by the agent. Because nothing
scoped the mentioned uid to the caller's org, a user in org A could comment on
their own item with `@opensweep look at @[x](finding:<ORG_B_UID>)` and
exfiltrate org B's finding/ticket/PR/doc/run.

WHAT: `render_mentioned_items` takes the set of repository uids the caller's
org may see and drops any mentioned item (or ticket-group proposal) whose
`repository_uid` is not in that set. In-org mentions still render. DB-free:
subject resolution is monkeypatched.
"""

import pytest

import domains.comments.service as svc_mod
from domains.comments.schemas import CommentSubjectType

pytestmark = pytest.mark.asyncio


class _Subject:
    def __init__(self, uid, repository_uid, title="item"):
        self.uid = uid
        self.repository_uid = repository_uid
        self.title = title


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    # get_subject returns our fake nodes keyed by uid; subject_snapshot is a
    # trivial formatter so we can assert on the rendered text.
    subjects = {
        "f-a": _Subject("f-a", "repo-a", "A finding"),
        "f-b": _Subject("f-b", "repo-b", "B finding"),
    }

    async def fake_get_subject(subject_type, uid):
        return subjects.get(uid)

    def fake_snapshot(subject_type, subject):
        return f"{subject_type.value} {subject.uid}: {subject.title}"

    monkeypatch.setattr(svc_mod, "get_subject", fake_get_subject)
    monkeypatch.setattr(svc_mod, "subject_snapshot", fake_snapshot)
    yield


async def test_drops_out_of_org_mention():
    refs = [
        {"type": CommentSubjectType.FINDING.value, "uid": "f-a", "label": "A"},
        {"type": CommentSubjectType.FINDING.value, "uid": "f-b", "label": "B"},
    ]
    # Caller's org owns only repo-a.
    rendered = await svc_mod.render_mentioned_items(refs, allowed_repo_uids={"repo-a"})
    assert "f-a" in rendered
    assert "f-b" not in rendered  # org-B finding must not leak into the prompt


async def test_in_org_mentions_still_render():
    refs = [{"type": CommentSubjectType.FINDING.value, "uid": "f-a", "label": "A"}]
    rendered = await svc_mod.render_mentioned_items(refs, allowed_repo_uids={"repo-a"})
    assert "A finding" in rendered


async def test_empty_org_scope_drops_everything():
    # A subject whose repository has no resolvable org (empty scope) must fail
    # closed — no snapshots rendered.
    refs = [{"type": CommentSubjectType.FINDING.value, "uid": "f-a", "label": "A"}]
    rendered = await svc_mod.render_mentioned_items(refs, allowed_repo_uids=set())
    assert rendered == ""
