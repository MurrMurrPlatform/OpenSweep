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


# The list query no longer scans the Event label and filters in Python — it
# asks Neo4j for the visible window directly. So the tenancy rule is pinned
# on `visibility_scope`, which is what the WHERE clause is built from, plus
# one check that a caller denied everything never reaches the database.

def _scope(*, platform, repository_uid=None, allowed=frozenset({"repo-a"})):
    return audit_mod.visibility_scope(
        allowed_repos=set(allowed),
        is_platform_admin=platform,
        repository_uid=repository_uid,
    )


def test_org_admin_cannot_see_platform_level_events():
    repos, platform_events = _scope(platform=False)
    assert repos == ["repo-a"]
    assert platform_events is False  # events with no repository stay hidden


def test_platform_admin_sees_platform_level_events():
    repos, platform_events = _scope(platform=True)
    assert repos == ["repo-a"]
    assert platform_events is True


def test_repository_uid_filter_scopes_the_list():
    # allowed_repos is {"repo-a"}; a second repo in the org would also be
    # visible, so the filter (not tenancy) must do the narrowing.
    assert _scope(platform=False, repository_uid="repo-a") == (["repo-a"], False)
    # A repo outside the org narrows to nothing rather than falling back.
    assert _scope(platform=False, repository_uid="repo-b") == ([], False)
    # Even for a platform admin, naming a repo excludes platform-level events.
    assert _scope(platform=True, repository_uid="repo-a") == (["repo-a"], False)


async def test_a_caller_with_no_visible_repos_never_queries(monkeypatch):
    async def no_repos(org_uid):
        return set()

    def boom(*a, **kw):
        raise AssertionError("queried the audit log with nothing visible")

    monkeypatch.setattr(audit_mod, "org_repo_uids", no_repos)
    monkeypatch.setattr(audit_mod.adb, "cypher_query", boom)
    listed = await audit_mod.list_events(
        subject_type=None, subject_uid=None, kind=None, actor_uid=None,
        repository_uid=None, limit=100, offset=0, user=_user(platform=False))
    assert listed == []


async def test_get_platform_event_404s_for_org_admin():
    from fastapi import HTTPException

    _seed("plat-1", "")
    with pytest.raises(HTTPException) as exc:
        await audit_mod.get_event("plat-1", user=_user(platform=False))
    assert exc.value.status_code == 404  # existence never leaks
