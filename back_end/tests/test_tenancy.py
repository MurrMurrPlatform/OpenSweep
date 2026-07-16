"""Multi-tenancy phase 2 — tenancy helpers, org resolution, and scope rules.

DB-free: neomodel/adb access is monkeypatched with fakes. Route-level
enforcement is spread across the routers (each fetch-check-act site); these
tests pin the primitives every route relies on.
"""

import pytest
from fastapi import HTTPException

import domains.tenancy as tenancy
from domains.organizations.models import LOCAL_ORG_UID
from domains.users.services.local_user import get_local_user

pytestmark = pytest.mark.asyncio


class _FakeAdb:
    """cypher_query fake: org→repos map, [(uid,)] rows."""

    def __init__(self, org_repos: dict[str, set[str]]):
        self.org_repos = org_repos

    async def cypher_query(self, query: str, params: dict | None = None):
        params = params or {}
        if "org_uid: $org" in query and "uid: $uid" in query:  # repo_in_org
            org = params["org"]
            return ([(1,)] if params["uid"] in self.org_repos.get(org, set()) else []), None
        if "org_uid: $org" in query:  # org_repo_uids
            return [(uid,) for uid in sorted(self.org_repos.get(params["org"], set()))], None
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture
def fake_graph(monkeypatch):
    adb = _FakeAdb({"org-a": {"repo-1", "repo-2"}, "org-b": {"repo-3"}})
    monkeypatch.setattr(tenancy, "adb", adb)
    return adb


# ── tenancy helpers ──────────────────────────────────────────────────────────


async def test_org_repo_uids(fake_graph):
    assert await tenancy.org_repo_uids("org-a") == {"repo-1", "repo-2"}
    assert await tenancy.org_repo_uids("org-b") == {"repo-3"}
    assert await tenancy.org_repo_uids("org-x") == set()


async def test_require_repo_in_org_passes_same_org(fake_graph):
    await tenancy.require_repo_in_org("repo-1", "org-a")  # no raise


async def test_require_repo_in_org_404_cross_org(fake_graph):
    with pytest.raises(HTTPException) as exc:
        await tenancy.require_repo_in_org("repo-3", "org-a")
    assert exc.value.status_code == 404
    assert exc.value.detail == "not found"  # existence never leaks


async def test_require_repo_in_org_404_unknown_and_empty(fake_graph):
    with pytest.raises(HTTPException):
        await tenancy.require_repo_in_org("nope", "org-a")
    with pytest.raises(HTTPException):
        await tenancy.require_repo_in_org("", "org-a")
    with pytest.raises(HTTPException):
        await tenancy.require_repo_in_org(None, "org-a")


# ── local user org ───────────────────────────────────────────────────────────


async def test_local_user_lives_in_local_org():
    assert get_local_user().org_uid == LOCAL_ORG_UID


# ── OIDC org resolution ──────────────────────────────────────────────────────


class _FakeNodes:
    def __init__(self):
        self.store: dict[str, object] = {}

    async def get_or_none(self, uid: str):
        return self.store.get(uid)


class _FakeNode:
    created_at = None

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.saved = 0

    async def save(self):
        self.saved += 1


@pytest.fixture
def oidc_module(monkeypatch):
    import domains.users.services.oidc_user as mod

    users, orgs = _FakeNodes(), _FakeNodes()

    class FakeUser(_FakeNode):
        nodes = users

        async def save(self):
            await super().save()
            users.store[self.uid] = self
            return self

    created_orgs: list = []

    class FakeOrg(_FakeNode):
        nodes = orgs

    async def fake_create_personal_org(*, creator_uid, display_name, email):
        org = FakeOrg(uid=f"org-of-{creator_uid}", name=f"{display_name}'s organization")
        orgs.store[org.uid] = org
        created_orgs.append(org)
        return org

    async def no_invitation(email):
        return None

    accepted: list = []

    async def fake_mark_accepted(inv, uid):
        accepted.append((inv, uid))

    async def no_userinfo(token):
        return {}

    async def fake_member_count(org_uid):
        return sum(1 for u in users.store.values() if getattr(u, "org_uid", "") == org_uid)

    monkeypatch.setattr(mod, "User", FakeUser)
    monkeypatch.setattr(mod, "Organization", FakeOrg)
    monkeypatch.setattr(mod, "create_personal_org", fake_create_personal_org)
    monkeypatch.setattr(mod, "find_pending_invitation", no_invitation)
    monkeypatch.setattr(mod, "mark_invitation_accepted", fake_mark_accepted)
    monkeypatch.setattr(mod, "org_member_count", fake_member_count)
    monkeypatch.setattr(mod, "fetch_userinfo", no_userinfo)
    monkeypatch.setattr(mod, "_cache", {})
    mod._test_created_orgs = created_orgs
    mod._test_accepted = accepted
    mod._test_users = users
    mod._test_orgs = orgs
    return mod


async def test_new_oidc_user_gets_personal_org(oidc_module):
    """Org-per-new-user: no org claim needed — first login provisions a
    personal org and the user owns it."""
    dto = await oidc_module.resolve_oidc_user(
        {"sub": "u1", "email": "a@b.c", "name": "A"}, "token"
    )
    assert dto.org_uid == "org-of-u1"
    assert dto.org_role == "owner"
    assert dto.role == "admin"
    assert dto.onboarded is False
    assert [o.uid for o in oidc_module._test_created_orgs] == ["org-of-u1"]


async def test_new_oidc_user_joins_org_via_invitation(oidc_module, monkeypatch):
    inv = _FakeNode(uid="inv-1", org_uid="org-acme", role="viewer", email="a@b.c")
    oidc_module._test_orgs.store["org-acme"] = _FakeNode(uid="org-acme", name="Acme")

    async def pending(email):
        return inv if email == "a@b.c" else None

    monkeypatch.setattr(oidc_module, "find_pending_invitation", pending)
    dto = await oidc_module.resolve_oidc_user(
        {"sub": "u2", "email": "a@b.c", "name": "A"}, "token"
    )
    assert dto.org_uid == "org-acme"
    assert dto.org_role == "member"
    assert dto.role == "viewer"  # from the invitation
    assert dto.onboarded is True  # joined an existing org — no welcome flow
    assert oidc_module._test_accepted == [(inv, "u2")]
    assert oidc_module._test_created_orgs == []  # no personal org


async def test_existing_user_keeps_opensweep_managed_role(oidc_module):
    """Zitadel project roles no longer drive the in-org role — owners manage
    it in OpenSweep. Only the platform-admin flag comes from the token."""
    oidc_module._test_users.store["u3"] = oidc_module.User(
        uid="u3",
        email="old@b.c",
        display_name="Old Name",
        role="viewer",
        org_uid="org-existing",
        org_role="member",
        onboarded=True,
        idp_org_id="",
    )
    claims = {
        "sub": "u3",
        "email": "new@b.c",
        "name": "Claim Name",
        "urn:zitadel:iam:org:project:roles": {"admin": {}},
    }
    dto = await oidc_module.resolve_oidc_user(claims, "token")
    assert dto.role == "viewer"  # NOT overwritten by the token's admin role
    assert dto.org_uid == "org-existing"
    assert dto.email == "new@b.c"  # identity refresh
    assert dto.display_name == "Old Name"  # user-managed, not clobbered
    assert dto.is_platform_admin is True  # the one claim with authz meaning


async def test_idp_org_join_seam_is_opt_in_and_least_privilege(oidc_module, monkeypatch):
    """H3: the IdP-org join seam (join an existing Organization keyed by the
    Zitadel resourceowner id) is OFF by default — a new user always gets a
    personal org, so the IdP-controlled claim can't drop them into another
    tenant. When an operator opts in, the joiner's role is NEVER taken from the
    token: only the bootstrapping first member is owner/admin; later joiners
    are least-privilege viewers."""
    oidc_module._test_orgs.store["z-org-1"] = _FakeNode(uid="z-org-1", name="Legacy")

    # Default (flag off): personal org, NOT the pre-existing tenant.
    monkeypatch.setattr(oidc_module.settings, "OPENSWEEP_ALLOW_IDP_ORG_JOIN", False)
    solo = await oidc_module.resolve_oidc_user(
        {
            "sub": "u5",
            "email": "solo@b.c",
            "name": "Solo",
            "urn:zitadel:iam:user:resourceowner:id": "z-org-1",
        },
        "token",
    )
    assert solo.org_uid == "org-of-u5"  # own personal org
    assert solo.org_role == "owner"

    # Opt in: first member of the empty legacy org bootstraps ownership.
    monkeypatch.setattr(oidc_module.settings, "OPENSWEEP_ALLOW_IDP_ORG_JOIN", True)
    first = await oidc_module.resolve_oidc_user(
        {
            "sub": "u6",
            "email": "first@b.c",
            "name": "First",
            "urn:zitadel:iam:user:resourceowner:id": "z-org-1",
        },
        "token",
    )
    assert first.org_uid == "z-org-1"
    assert first.org_role == "owner"
    assert first.role == "admin"

    # A later joiner is a least-privilege viewer — the token's maintainer claim
    # must NOT confer an in-org capability role.
    second = await oidc_module.resolve_oidc_user(
        {
            "sub": "u7",
            "email": "second@b.c",
            "name": "Second",
            "urn:zitadel:iam:user:resourceowner:id": "z-org-1",
            "urn:zitadel:iam:org:project:roles": {"maintainer": {}},
        },
        "token",
    )
    assert second.org_uid == "z-org-1"
    assert second.org_role == "member"
    assert second.role == "viewer"


async def test_placeholder_identity_heals_on_next_login(oidc_module):
    """A user provisioned while profile claims were unreachable (userinfo
    down) carries `{sub}@zitadel.local` everywhere. Once real claims arrive,
    email, display_name AND the placeholder-named personal org all heal."""
    placeholder = "u7@zitadel.local"
    oidc_module._test_orgs.store["org-of-u7"] = _FakeNode(
        uid="org-of-u7", name=f"{placeholder}'s organization"
    )
    oidc_module._test_users.store["u7"] = oidc_module.User(
        uid="u7",
        email=placeholder,
        display_name=placeholder,
        role="admin",
        org_uid="org-of-u7",
        org_role="owner",
        onboarded=False,
        idp_org_id="",
    )
    dto = await oidc_module.resolve_oidc_user(
        {"sub": "u7", "email": "jeroen@example.com", "name": "Jeroen Brouns"}, "token"
    )
    assert dto.email == "jeroen@example.com"
    assert dto.display_name == "Jeroen Brouns"
    assert oidc_module._test_orgs.store["org-of-u7"].name == "Jeroen Brouns's organization"


async def test_placeholder_heal_never_renames_custom_org_name(oidc_module):
    """If the owner already renamed their org, the heal fixes the identity
    but leaves the org name alone."""
    placeholder = "u8@zitadel.local"
    oidc_module._test_orgs.store["org-of-u8"] = _FakeNode(uid="org-of-u8", name="Acme Corp")
    oidc_module._test_users.store["u8"] = oidc_module.User(
        uid="u8",
        email=placeholder,
        display_name=placeholder,
        role="admin",
        org_uid="org-of-u8",
        org_role="owner",
        onboarded=True,
        idp_org_id="",
    )
    dto = await oidc_module.resolve_oidc_user(
        {"sub": "u8", "email": "j@example.com", "name": "J"}, "token"
    )
    assert dto.display_name == "J"
    assert oidc_module._test_orgs.store["org-of-u8"].name == "Acme Corp"


async def test_platform_admin_flag_defaults_false(oidc_module):
    dto = await oidc_module.resolve_oidc_user(
        {"sub": "u4", "email": "d@b.c", "name": "D"}, "token"
    )
    assert dto.is_platform_admin is False


# ── platform-tool scope (run tokens pinned to their run's repo) ─────────────


class _Conn:
    def __init__(self, run_token_uid: str = ""):
        self.scope = {"state": {"run_token_uid": run_token_uid} if run_token_uid else {}}


@pytest.fixture
def scope_module(monkeypatch):
    import api.platform_scope as mod

    async def fake_run_repo(run_uid: str) -> str:
        return {"run-1": "repo-1"}.get(run_uid, "")

    monkeypatch.setattr(mod, "_run_repository_uid", fake_run_repo)

    calls: list[tuple] = []

    async def fake_require(repo, org):
        calls.append((repo, org))
        if repo != "repo-1" or org != "org-a":
            raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(mod, "require_repo_in_org", fake_require)
    mod._test_calls = calls
    return mod


def _user(org="org-a"):
    from domains.users.schemas import UserDTO

    return UserDTO(uid="u", email="e@x.y", display_name="U", role="admin", org_uid=org)


async def test_run_token_pinned_to_own_repo(scope_module):
    await scope_module.require_tool_repo_access(_Conn("run-1"), _user(), "repo-1")  # ok
    with pytest.raises(HTTPException):  # other repo, even same org → 404
        await scope_module.require_tool_repo_access(_Conn("run-1"), _user(), "repo-2")


async def test_run_token_pinned_to_own_run(scope_module):
    await scope_module.require_tool_run_access(_Conn("run-1"), _user(), "run-1")  # ok
    with pytest.raises(HTTPException):
        await scope_module.require_tool_run_access(_Conn("run-1"), _user(), "run-9")


async def test_human_caller_uses_org_rules(scope_module):
    await scope_module.require_tool_repo_access(_Conn(), _user("org-a"), "repo-1")  # ok
    with pytest.raises(HTTPException):
        await scope_module.require_tool_repo_access(_Conn(), _user("org-b"), "repo-1")
    assert scope_module._test_calls  # delegated to require_repo_in_org
