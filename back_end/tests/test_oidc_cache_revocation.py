"""F6 (MEDIUM) — the platform-admin bit must not survive in the resolve cache.

WHY: `resolve_oidc_user` cached the whole UserDTO (incl. is_platform_admin) for
60s and returned it verbatim on a cache hit. So if an operator's `admin`
project role was removed at the IdP, they kept platform-admin for up to a
minute. The platform-admin decision is derived purely from the current
request's (freshly signature-verified) claims, so it should be recomputed on
every call rather than served stale.

WHAT: priming the cache with an admin DTO and then resolving the SAME subject
with claims that no longer assert `admin` must return is_platform_admin=False
immediately — without waiting for the TTL. Org/role fields (owner-managed) may
still come from cache. DB-free: the cache hit short-circuits before any DB
access.
"""

import time

import pytest

import domains.users.services.oidc_user as ou
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_cache():
    ou.invalidate_user_cache()
    yield
    ou.invalidate_user_cache()


def _dto(platform):
    return UserDTO(
        uid="sub1", email="e@x.y", display_name="U", role="admin",
        org_uid="org-1", org_role="owner", is_platform_admin=platform,
    )


async def test_platform_admin_recomputed_on_cache_hit():
    # Prime the cache as though the last login was an operator.
    ou._cache["sub1"] = (time.monotonic() + ou._TTL, _dto(platform=True))
    # This request's claims no longer carry the admin role.
    result = await ou.resolve_oidc_user({"sub": "sub1"})
    assert result.is_platform_admin is False
    # Cached org identity is still honored (no DB hit needed).
    assert result.org_uid == "org-1"


async def test_platform_admin_still_granted_when_claim_present():
    ou._cache["sub1"] = (time.monotonic() + ou._TTL, _dto(platform=False))
    result = await ou.resolve_oidc_user(
        {"sub": "sub1", "urn:zitadel:iam:org:project:roles": {"admin": {"o": "org"}}}
    )
    assert result.is_platform_admin is True
