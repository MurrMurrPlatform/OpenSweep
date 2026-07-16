"""Resolve verified OIDC claims to a User node (upsert + short TTL cache).

Called by api.dependencies.get_current_user when TokenAuthMiddleware stashed
`oidc_claims` in the request scope. The Zitadel `sub` is the User.uid.

Zitadel is authentication only — org membership and the in-org capability
role live in OpenSweep:
  - Known user: identity fields (email) refresh from the token; org_uid,
    role, org_role, display_name come from the User node (owner-managed).
  - Unknown user: provisioned via domains/organizations/services/provisioning
    — join the org of a pending email invitation, or get a fresh personal
    org as its owner (org-per-new-user).

The only claim with authorization meaning is the Zitadel project role
`admin`: it marks the instance operator (UserDTO.is_platform_admin) and is
re-derived from the token on every cache miss — it is never stored.
"""

import asyncio
import time

from config import settings
from domains.organizations.models import Organization
from domains.organizations.services.provisioning import (
    create_personal_org,
    find_pending_invitation,
    mark_invitation_accepted,
    org_member_count,
    personal_org_name,
)
from domains.users.models import User
from domains.users.schemas import UserDTO
from infrastructure.oidc import (
    fetch_userinfo,
    is_platform_admin_claim,
    primary_org_id,
)
from logging_config import logger

_TTL = 60.0
_cache: dict[str, tuple[float, UserDTO]] = {}
_provision_lock = asyncio.Lock()  # first-login provisioning is rare — serialize it


def invalidate_user_cache(uid: str = "") -> None:
    """Bust the resolve cache after membership/role mutations so changes made
    through the org API take effect immediately, not after _TTL."""
    if uid:
        _cache.pop(uid, None)
    else:
        _cache.clear()


def _to_dto(user: User, *, is_platform_admin: bool) -> UserDTO:
    return UserDTO(
        uid=user.uid,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        org_uid=user.org_uid,
        org_role=user.org_role,
        is_platform_admin=is_platform_admin,
        onboarded=bool(user.onboarded),
        created_at=user.created_at,
    )


async def _provision_new_user(
    *, sub: str, email: str, name: str, idp_org_id: str, claims: dict
) -> User:
    """First login: invitation match joins that org; a pre-existing legacy
    org keyed by the Zitadel org id (phase-2 scheme, where the IdP org WAS
    the OpenSweep org) is joined for backward compatibility; otherwise the user
    gets a personal org and owns it."""
    inv = await find_pending_invitation(email)
    if inv is not None and await Organization.nodes.get_or_none(uid=inv.org_uid):
        user = await User(
            uid=sub,
            email=email,
            display_name=name,
            role=inv.role,
            org_uid=inv.org_uid,
            org_role="member",
            onboarded=True,  # joining an existing org — nothing to set up
            idp_org_id=idp_org_id,
        ).save()
        await mark_invitation_accepted(inv, sub)
        logger.info(f"OIDC: {email} joined org {inv.org_uid} via invitation {inv.uid}")
        return user

    if (
        settings.OPENSWEEP_ALLOW_IDP_ORG_JOIN
        and idp_org_id
        and await Organization.nodes.get_or_none(uid=idp_org_id)
    ):
        # Second-deployment seam (H3), OFF by default. Only legacy/migrated
        # instances that provisioned Organization nodes keyed by the Zitadel
        # resourceowner id opt into "colleagues from the same IdP org land
        # together" (OPENSWEEP_ALLOW_IDP_ORG_JOIN). Without the flag the
        # attacker-influenceable resourceowner claim can never drop a new user
        # into an existing tenant. Even here the joiner's in-org capability
        # role is NEVER taken from the token — a non-first member is a
        # least-privilege viewer until an owner promotes them.
        first = await org_member_count(idp_org_id) == 0
        user = await User(
            uid=sub,
            email=email,
            display_name=name,
            role="admin" if first else "viewer",
            org_uid=idp_org_id,
            org_role="owner" if first else "member",
            onboarded=True,
            idp_org_id=idp_org_id,
        ).save()
        logger.info(f"OIDC: {email} joined pre-provisioned org {idp_org_id}")
        return user

    org = await create_personal_org(creator_uid=sub, display_name=name, email=email)
    user = await User(
        uid=sub,
        email=email,
        display_name=name,
        role="admin",
        org_uid=org.uid,
        org_role="owner",
        onboarded=False,  # run the welcome flow once
        idp_org_id=idp_org_id,
    ).save()
    logger.info(f"OIDC: created personal org {org.uid} for new user {email}")
    return user


async def resolve_oidc_user(claims: dict, access_token: str = "") -> UserDTO:
    sub = str(claims.get("sub", ""))
    now = time.monotonic()
    # Derived fresh from THIS request's (signature-verified) claims every call,
    # and pinned to OpenSweep's project (F5). Recomputed even on a cache hit
    # (F6) so an operator whose `admin` role was removed at the IdP loses
    # platform-admin immediately instead of after the cache TTL.
    is_platform_admin = is_platform_admin_claim(claims)
    hit = _cache.get(sub)
    if hit and hit[0] > now:
        cached = hit[1]
        if cached.is_platform_admin != is_platform_admin:
            cached = cached.model_copy(update={"is_platform_admin": is_platform_admin})
            _cache[sub] = (hit[0], cached)
        return cached
    email = str(claims.get("email", "") or "")
    name = str(claims.get("name", "") or claims.get("preferred_username", "") or "")
    idp_org_id = primary_org_id(claims)
    if (not email or not name) and access_token:
        # JWT access tokens don't always mint profile claims — ask userinfo
        # before falling back (email drives invitation matching).
        info = await fetch_userinfo(access_token)
        email = email or str(info.get("email", "") or "")
        name = name or str(info.get("name", "") or info.get("preferred_username", "") or "")
        idp_org_id = idp_org_id or primary_org_id(info)
    email = email or f"{sub}@zitadel.local"
    name = name or email

    user = await User.nodes.get_or_none(uid=sub)
    if user is None:
        async with _provision_lock:
            user = await User.nodes.get_or_none(uid=sub)  # lost the race?
            if user is None:
                user = await _provision_new_user(
                    sub=sub, email=email, name=name, idp_org_id=idp_org_id, claims=claims
                )
    else:
        # Identity refresh only — email may change in the IdP. display_name is
        # user-managed (account settings), role/org are owner-managed.
        dirty = False
        placeholder = f"{sub}@zitadel.local"
        if user.display_name == placeholder and name != placeholder:
            # Provisioned while profile claims were unreachable (userinfo
            # down): heal the placeholder identity, and the personal org that
            # was named after it — a user-chosen org name never matches the
            # placeholder-derived name, so renames can't clobber one.
            org = await Organization.nodes.get_or_none(uid=user.org_uid)
            if org is not None and org.name == personal_org_name(
                user.display_name, user.email
            ):
                org.name = personal_org_name(name, email)
                await org.save()
            user.display_name = name
            dirty = True
            logger.info(f"OIDC: healed placeholder identity for {sub} → {email}")
        if (user.email, user.idp_org_id) != (email, idp_org_id):
            user.email = email
            user.idp_org_id = idp_org_id
            dirty = True
        if dirty:
            await user.save()
        if not user.org_uid:
            # Startup migration normally covers this; self-heal just in case.
            async with _provision_lock:
                user = await User.nodes.get(uid=sub)
                if not user.org_uid:
                    org = await create_personal_org(
                        creator_uid=sub, display_name=user.display_name, email=user.email
                    )
                    user.org_uid = org.uid
                    user.org_role = "owner"
                    user.role = "admin"
                    await user.save()

    if is_platform_admin and user.org_uid:
        # Zero-step OSS setup: a GITHUB_TOKEN already in the environment
        # becomes the operator org's PAT connection on their first login.
        # Never raises; no-ops instantly once settled or without a token.
        from domains.organizations.services.git_connections import (
            ensure_env_pat_connection,
        )

        await ensure_env_pat_connection(user.org_uid, linked_by=user.uid)

    dto = _to_dto(user, is_platform_admin=is_platform_admin)
    _cache[sub] = (now + _TTL, dto)
    return dto
