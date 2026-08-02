"""Org membership provisioning — the write side of multi-tenancy.

First OIDC login of an unknown user:
  - a pending OrgInvitation matching their email (case-insensitive) attaches
    them to the inviting org with the invited role, or
  - a personal org is created and they become its owner (org_role=owner,
    role=admin, onboarded=False so the frontend runs the welcome flow).

Already-registered users can accept later invitations explicitly
(accept_invitation) — they move orgs, and their previous org is deleted when
it ends up empty (no members, no repositories).

migrate_tenancy() runs on every backend startup and is idempotent: it only
touches users whose org_uid is unset (pre-membership deployments carried the
Zitadel org in idp_org_id), repairs owner-less orgs, ensures the local org
node exists, and backfills GitConnection links from repositories that
already reference an installation. First and repeat deploys converge to the
same state.
"""

from datetime import UTC, datetime, timedelta

from neomodel import adb

from domains.organizations.models import (
    LOCAL_ORG_UID,
    GitConnection,
    Organization,
    OrgInvitation,
    new_org_uid,
)
from domains.users.models import User
from logging_config import logger


def personal_org_name(display_name: str, email: str) -> str:
    base = (display_name or "").strip() or (email or "").split("@")[0].strip()
    return f"{base}'s organization" if base else "My organization"


async def ensure_organization(org_uid: str, name_hint: str = "", created_by: str = "") -> Organization:
    org = await Organization.nodes.get_or_none(uid=org_uid)
    if org is None:
        org = await Organization(
            uid=org_uid, name=name_hint or org_uid, created_by=created_by
        ).save()
    return org


async def create_personal_org(*, creator_uid: str, display_name: str, email: str) -> Organization:
    return await Organization(
        uid=new_org_uid(),
        name=personal_org_name(display_name, email),
        created_by=creator_uid,
    ).save()


#: An invitation is a standing grant to join an org on a matching email. Left
#: immortal, a stale invitation lets someone who later controls that address
#: join a tenant they were never meant to. created_at is stored as a numeric
#: epoch (neomodel DateTimeProperty), so the window is a plain epoch compare.
INVITATION_TTL = timedelta(days=7)


async def find_pending_invitation(email: str) -> OrgInvitation | None:
    """Oldest unexpired pending invitation for this email (case-insensitive)."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    min_epoch = (datetime.now(UTC) - INVITATION_TTL).timestamp()
    rows, _ = await adb.cypher_query(
        "MATCH (i:OrgInvitation {email: $email, status: 'pending'}) "
        "WHERE i.created_at >= $min_epoch "
        "RETURN i ORDER BY i.created_at LIMIT 1",
        {"email": normalized, "min_epoch": min_epoch},
    )
    return OrgInvitation.inflate(rows[0][0]) if rows else None


async def pending_invitations_for_email(email: str) -> list[OrgInvitation]:
    normalized = (email or "").strip().lower()
    if not normalized:
        return []
    rows, _ = await adb.cypher_query(
        "MATCH (i:OrgInvitation {email: $email, status: 'pending'}) "
        "RETURN i ORDER BY i.created_at",
        {"email": normalized},
    )
    return [OrgInvitation.inflate(row[0]) for row in rows]


async def mark_invitation_accepted(inv: OrgInvitation, user_uid: str) -> None:
    inv.status = "accepted"
    inv.accepted_by = user_uid
    inv.accepted_at = datetime.now(UTC)
    await inv.save()


async def org_member_count(org_uid: str) -> int:
    rows, _ = await adb.cypher_query(
        "MATCH (u:User {org_uid: $org}) RETURN count(u)", {"org": org_uid}
    )
    return int(rows[0][0]) if rows else 0


async def org_owner_count(org_uid: str) -> int:
    rows, _ = await adb.cypher_query(
        "MATCH (u:User {org_uid: $org, org_role: 'owner'}) RETURN count(u)",
        {"org": org_uid},
    )
    return int(rows[0][0]) if rows else 0


async def delete_org_if_empty(org_uid: str) -> bool:
    """Remove an org that has no members and no repositories (e.g. the
    personal org left behind when its only user accepted an invitation).
    Never deletes the local org."""
    if not org_uid or org_uid == LOCAL_ORG_UID:
        return False
    rows, _ = await adb.cypher_query(
        "MATCH (o:Organization {uid: $org}) "
        "WHERE NOT EXISTS { MATCH (u:User {org_uid: $org}) } "
        "AND NOT EXISTS { MATCH (r:Repository {org_uid: $org}) } "
        "DETACH DELETE o RETURN count(*)",
        {"org": org_uid},
    )
    deleted = bool(rows and rows[0][0])
    if deleted:
        # Pending invitations into a deleted org can never be satisfied.
        await adb.cypher_query(
            "MATCH (i:OrgInvitation {org_uid: $org}) DETACH DELETE i", {"org": org_uid}
        )
    return deleted


async def move_user_to_org(user: User, org_uid: str, *, role: str, org_role: str) -> User:
    """Reassign a user's org membership; cleans up the org left behind if it
    ends up empty. Used by invitation acceptance and member removal."""
    previous = user.org_uid
    user.org_uid = org_uid
    user.role = role
    user.org_role = org_role
    await user.save()
    if previous and previous != org_uid:
        await delete_org_if_empty(previous)
    return user


async def accept_invitation(user: User, inv: OrgInvitation) -> User:
    """Move an existing user into the inviting org."""
    await move_user_to_org(user, inv.org_uid, role=inv.role, org_role="member")
    await mark_invitation_accepted(inv, user.uid)
    return user


# ── Startup migration ────────────────────────────────────────────────────────


async def migrate_tenancy() -> None:
    """Idempotent tenancy provisioning migration — safe on every startup.

    Only ever fills gaps: users provisioned by current code always carry
    org_uid/org_role, so re-runs are no-ops for them.
    """
    # The local org must exist for the org-management API in no-auth mode.
    await ensure_organization(LOCAL_ORG_UID, "Local organization")

    # 1. Pre-membership users: org_uid was implied by idp_org_id (Zitadel
    #    resourceowner) or the local org. Stamp it, mark them onboarded, and
    #    make pre-existing admins owners of their org.
    rows, _ = await adb.cypher_query(
        "MATCH (u:User) WHERE u.org_uid IS NULL OR u.org_uid = '' "
        "SET u.org_uid = coalesce(nullif(u.idp_org_id, ''), $local), "
        "    u.org_role = CASE WHEN u.role = 'admin' THEN 'owner' ELSE 'member' END, "
        "    u.onboarded = true "
        "RETURN count(u)",
        {"local": LOCAL_ORG_UID},
    )
    stamped = int(rows[0][0]) if rows else 0
    if stamped:
        logger.info(f"Tenancy migration: stamped org membership on {stamped} user(s)")

    # 2. Organization nodes for every org_uid in use (users + repositories) —
    #    covers orgs referenced before the node was upserted.
    for label in ("User", "Repository"):
        await adb.cypher_query(
            f"MATCH (n:{label}) WHERE n.org_uid IS NOT NULL AND n.org_uid <> '' "
            "WITH DISTINCT n.org_uid AS org_uid "
            "MERGE (o:Organization {uid: org_uid}) "
            "ON CREATE SET o.name = org_uid, o.created_by = '', o.created_at = datetime()",
            {},
        )

    # 3. Owner-less orgs: promote the earliest member (invariant repair — the
    #    org API guards against demoting the last owner, but migrated data
    #    may start without one).
    rows, _ = await adb.cypher_query(
        "MATCH (u:User) WHERE u.org_uid IS NOT NULL AND u.org_uid <> '' "
        "WITH u ORDER BY u.created_at "
        "WITH u.org_uid AS org, collect(u) AS members "
        "WHERE none(m IN members WHERE m.org_role = 'owner') "
        "WITH org, members[0] AS promote "
        "SET promote.org_role = 'owner', promote.role = 'admin' "
        "RETURN count(*)",
        {},
    )
    promoted = int(rows[0][0]) if rows else 0
    if promoted:
        logger.info(f"Tenancy migration: promoted an owner in {promoted} org(s)")

    # 4. Pre-tenancy LLM providers belong to the local org — there is no
    #    shared/platform scope anymore; every provider is owned by exactly
    #    one organization.
    await adb.cypher_query(
        "MATCH (p:LLMProvider) WHERE p.org_uid IS NULL OR p.org_uid = '' "
        "SET p.org_uid = $local",
        {"local": LOCAL_ORG_UID},
    )

    # 5. GitConnection links from repositories that already carry an
    #    installation id (pre-link deployments) — first repo per installation
    #    wins; conflicts are logged, never guessed.
    rows, _ = await adb.cypher_query(
        "MATCH (r:Repository) "
        "WHERE r.github_installation_id IS NOT NULL AND r.org_uid IS NOT NULL "
        "AND r.org_uid <> '' "
        "RETURN DISTINCT r.github_installation_id, r.org_uid ORDER BY r.github_installation_id",
        {},
    )
    for raw_id, org_uid in rows:
        try:
            inst_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        existing = await GitConnection.nodes.get_or_none(
            provider="github", external_id=str(inst_id)
        )
        if existing is None:
            await GitConnection(
                provider="github", external_id=str(inst_id), org_uid=org_uid
            ).save()
            logger.info(
                f"Tenancy migration: linked GitHub installation {inst_id} → org {org_uid}"
            )
        elif existing.org_uid != org_uid:
            logger.warning(
                f"GitHub installation {inst_id} linked to org {existing.org_uid} but "
                f"repositories also exist in org {org_uid} — leaving the existing link"
            )
