"""F5/H3 (HIGH) — first-login must not drop a new user into an existing tenant.

WHY: on first login `_provision_new_user` would, if the token's Zitadel
`resourceowner:id` matched an existing Organization, JOIN that org — and take
the joiner's in-org capability role straight from the token. The
resourceowner claim is IdP-controlled, so this was a seam by which a new user
could land inside another tenant's org (and, as a non-first member, assert
maintainer/admin from the token). Fresh instances never mint those org nodes,
but migrated/dev-seeded ones do.

WHAT:
  - The join seam is OFF by default (OPENSWEEP_ALLOW_IDP_ORG_JOIN=False): a new
    user always gets their OWN personal org even when idp_org_id matches.
  - When an operator explicitly enables the seam, a non-first joiner is a
    least-privilege `viewer` (org_role member) — never the token's asserted
    role; only the bootstrapping first member is owner/admin.
DB-free: provisioning collaborators are monkeypatched.
"""

from types import SimpleNamespace

import pytest

import domains.users.services.oidc_user as ou

pytestmark = pytest.mark.asyncio

_ADMIN_CLAIMS = {"urn:zitadel:iam:org:project:roles": {"admin": {"o": "org"}}}


class _FakeUser:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        return self


class _OrgNodes:
    async def get_or_none(self, uid):
        return SimpleNamespace(uid=uid) if uid == "existing-org" else None


class _FakeOrg:
    nodes = _OrgNodes()


@pytest.fixture
def provisioning(monkeypatch):
    monkeypatch.setattr(ou, "User", _FakeUser)
    monkeypatch.setattr(ou, "Organization", _FakeOrg)

    async def no_invite(email):
        return None

    async def personal_org(creator_uid, display_name, email):
        return SimpleNamespace(uid="personal-org")

    member_count = {"n": 0}

    async def org_member_count(org_uid):
        return member_count["n"]

    monkeypatch.setattr(ou, "find_pending_invitation", no_invite)
    monkeypatch.setattr(ou, "create_personal_org", personal_org)
    monkeypatch.setattr(ou, "org_member_count", org_member_count)
    return member_count


async def _provision(idp_org_id="existing-org", claims=None):
    return await ou._provision_new_user(
        sub="sub1", email="e@x.y", name="U", idp_org_id=idp_org_id, claims=claims or {}
    )


async def test_join_seam_off_by_default_gives_personal_org(provisioning, monkeypatch):
    monkeypatch.setattr(ou.settings, "OPENSWEEP_ALLOW_IDP_ORG_JOIN", False)
    user = await _provision(claims=_ADMIN_CLAIMS)
    # Never lands in the existing tenant — gets its own personal org, as owner.
    assert user.org_uid == "personal-org"
    assert user.org_role == "owner"


async def test_join_seam_on_non_first_member_is_least_privilege(provisioning, monkeypatch):
    monkeypatch.setattr(ou.settings, "OPENSWEEP_ALLOW_IDP_ORG_JOIN", True)
    provisioning["n"] = 1  # org already has a member → this user is NOT first
    user = await _provision(claims=_ADMIN_CLAIMS)
    assert user.org_uid == "existing-org"
    assert user.org_role == "member"
    assert user.role == "viewer"  # NOT taken from the token's admin claim


async def test_join_seam_on_first_member_bootstraps_owner(provisioning, monkeypatch):
    monkeypatch.setattr(ou.settings, "OPENSWEEP_ALLOW_IDP_ORG_JOIN", True)
    provisioning["n"] = 0  # empty org → first member bootstraps ownership
    user = await _provision(claims={})
    assert user.org_uid == "existing-org"
    assert user.org_role == "owner"
    assert user.role == "admin"
