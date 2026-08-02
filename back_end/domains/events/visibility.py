"""Who may see which audit Event — one definition, two surfaces.

The raw log (api/v1/audit.py) and the in-app inbox
(domains/notifications/service.py) ask the same question of the same nodes,
and both now ask it in Cypher rather than by filtering a scan afterwards. The
rule lives here so there is exactly one copy of it:

  - an Event carrying a repository is visible to that repository's org;
  - an Event carrying none is platform-level (provider/app config, run-policy
    edits, kill-switch toggles) and belongs to the instance operator.

That second clause MUST key off `is_platform_admin`, the Zitadel
instance-operator flag, and never the in-org capability role (F3): every user
who creates a personal org is provisioned role="admin", so an org role would
expose instance-wide events to any tenant.

`event_is_visible` (per node) and `visible_clause` (in the query) are two
expressions of that one rule and must agree. A stored `repository_uid` may be
null or "" — both mean "no repository", which is why the Cypher coalesces.
"""

from __future__ import annotations

from typing import Any


def visibility_scope(
    *, allowed_repos: set[str], is_platform_admin: bool, repository_uid: str | None
) -> tuple[list[str], bool]:
    """What a caller may read: (repository uids, include platform events).

    Truthiness, not `is not None`: `?repository_uid=` binds "" and has always
    meant "no repo filter", the same reading tenancy.repo_scope uses.
    """
    if repository_uid:
        # A repo the caller cannot see narrows to nothing rather than falling
        # back to the whole org. It never widens to platform events either —
        # those belong to no repository.
        return ([repository_uid] if repository_uid in allowed_repos else [], False)
    return sorted(allowed_repos), is_platform_admin


def event_is_visible(event: Any, allowed_repos: set[str], is_platform_admin: bool) -> bool:
    """The same rule for a single already-loaded Event."""
    repo = getattr(event, "repository_uid", "") or ""
    if repo:
        return repo in allowed_repos
    return is_platform_admin


def visible_clause(*, platform_events: bool, var: str = "e") -> str:
    """The WHERE fragment for `visibility_scope`'s result.

    Expects a `$repos` parameter. `null IN $repos` evaluates to null (not
    true), so an Event with no repository is excluded unless the caller is an
    operator and the coalesce arm is present.
    """
    clause = f"{var}.repository_uid IN $repos"
    if platform_events:
        clause += f" OR coalesce({var}.repository_uid, '') = ''"
    return f"({clause})"


# Cypher sorts null ABOVE every value on DESC, so an Event predating
# `occurred_at` (or written by raw Cypher) would pin itself to the top of
# every feed forever. The Python sorts these queries replaced treated a
# missing date as the oldest possible.
NEWEST_FIRST = "ORDER BY {var}.occurred_at IS NULL, {var}.occurred_at DESC"


def newest_first(var: str = "e") -> str:
    return NEWEST_FIRST.format(var=var)
