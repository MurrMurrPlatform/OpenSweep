"""F3 (HIGH) — platform-level audit events are instance-operator-only.

WHY: `list_events`/`get_event` gated platform-level Events (those with no
`repository_uid` — LLM-provider config, org membership, GitHub installations,
run-policy edits, kill-switch toggles) on `role_at_least(user.role, "admin")`.
But `user.role` is the in-ORG capability role, and every user who creates a
personal org is provisioned `role="admin"`. So any ordinary tenant could read
instance-wide audit events belonging to OTHER organizations. The gate must be
`user.is_platform_admin` (the Zitadel instance-operator flag).

WHAT: an org admin (role="admin", is_platform_admin=False) sees only their own
org's repo-scoped events, never platform-level ones; the platform admin sees
platform-level events. Repo-scoped filtering is unchanged. DB-free.
"""

import pytest

import api.v1.audit as audit_mod
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


class _Event:
    def __init__(self, uid, repository_uid, kind="k"):
        self.uid = uid
        self.repository_uid = repository_uid
        self.kind = kind
        self.subject_uid = "s"
        self.subject_type = "T"
        self.actor_uid = "a"
        self.payload = {}
        self.occurred_at = None


_EVENTS: list[_Event] = []


class _EventNodes:
    async def all(self):
        return list(_EVENTS)

    async def get_or_none(self, **kw):
        for e in _EVENTS:
            if all(getattr(e, k, None) == v for k, v in kw.items()):
                return e
        return None


class FakeEvent:
    nodes = _EventNodes()


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _EVENTS.clear()
    monkeypatch.setattr(audit_mod, "Event", FakeEvent)

    async def fake_org_repo_uids(org_uid):
        return {"repo-a"} if org_uid == "org-a" else set()

    monkeypatch.setattr(audit_mod, "org_repo_uids", fake_org_repo_uids)
    yield
    _EVENTS.clear()


def _user(*, platform, role="admin", org="org-a"):
    return UserDTO(
        uid="u", email="e@x.y", display_name="U", role=role,
        org_uid=org, org_role="owner", is_platform_admin=platform,
    )


def _seed(uid, repo):
    _EVENTS.append(_Event(uid, repo))


async def test_org_admin_cannot_see_platform_level_events():
    _seed("plat-1", "")        # platform-level (no repository)
    _seed("mine-1", "repo-a")  # my org's repo
    listed = await audit_mod.list_events(
        subject_type=None, subject_uid=None, kind=None, actor_uid=None, limit=100, user=_user(platform=False))
    uids = {e.uid for e in listed}
    assert uids == {"mine-1"}  # the platform-level event must NOT appear


async def test_platform_admin_sees_platform_level_events():
    _seed("plat-1", "")
    listed = await audit_mod.list_events(
        subject_type=None, subject_uid=None, kind=None, actor_uid=None, limit=100, user=_user(platform=True))
    assert {e.uid for e in listed} == {"plat-1"}


async def test_get_platform_event_404s_for_org_admin():
    from fastapi import HTTPException

    _seed("plat-1", "")
    with pytest.raises(HTTPException) as exc:
        await audit_mod.get_event("plat-1", user=_user(platform=False))
    assert exc.value.status_code == 404  # existence never leaks
