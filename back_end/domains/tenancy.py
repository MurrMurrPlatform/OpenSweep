"""Org tenancy enforcement (multi-tenancy phase 2).

Repository is the tenancy boundary: it carries org_uid, and every other
domain node carries repository_uid — so "may this user touch this thing"
reduces to "is the thing's repository in the user's org".

Usage in routes:
    await require_repo_in_org(entity.repository_uid, user.org_uid)   # 404
    uids = await repo_scope(repository_uid, user.org_uid)            # lists

404 (not 403) on cross-org access so existence of other tenants' resources
never leaks.

List routes pass `repo_scope(...)` down to the service, which puts it in the
query as `repository_uid__in=...`. Loading a whole label and dropping the
other tenants' rows in Python afterwards is not equivalent: it reads every
tenant's data to answer one tenant's question, and the cost grows with the
whole instance rather than with the caller's org.
"""

from fastapi import HTTPException
from neomodel import adb


async def org_repo_uids(org_uid: str) -> set[str]:
    """All repository uids in the org — for filtering cross-repo lists."""
    rows, _ = await adb.cypher_query(
        "MATCH (r:Repository {org_uid: $org}) RETURN r.uid", {"org": org_uid}
    )
    return {row[0] for row in rows}


async def repo_scope(repository_uid: str | None, org_uid: str) -> list[str]:
    """The repositories a list query may read, as a query-ready allow-list.

    Either the single repo the caller asked for — callers check that one
    with require_repo_in_org first — or every repo in their org. An empty
    result means the org owns no repositories, so the list is empty; it
    must never be read as "no filter".
    """
    if repository_uid:
        return [repository_uid]
    return sorted(await org_repo_uids(org_uid))


async def repo_in_org(repository_uid: str, org_uid: str) -> bool:
    rows, _ = await adb.cypher_query(
        "MATCH (r:Repository {uid: $uid, org_uid: $org}) RETURN 1",
        {"uid": repository_uid, "org": org_uid},
    )
    return bool(rows)


async def require_repo_in_org(repository_uid: str | None, org_uid: str) -> None:
    """404 unless the repository exists AND belongs to the org. Un-scoped
    entities (no repository_uid) are nobody's — also 404."""
    if not repository_uid or not await repo_in_org(repository_uid, org_uid):
        raise HTTPException(status_code=404, detail="not found")
