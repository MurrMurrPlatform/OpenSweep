"""Convenience read helpers for Findings."""

from domains.findings.models import Finding

# Statuses this lookup surfaces: everything except closed triage decisions
# (dismissed / wont-fix / superseded). Fixed/acknowledged/accepted stay
# visible so a re-report joins the existing history instead of forking a
# duplicate. Cypher-side positive filter — neomodel exposes `__in` but not
# `__nin`, so we enumerate the active complement.
_ACTIVE_STATUSES = ["open", "ticketed", "acknowledged", "fixed", "accepted"]


async def find_similar(
    *,
    repository_uid: str,
    dedupe_key: str | None = None,
    title_substring: str | None = None,
) -> list[Finding]:
    """Return active Findings that look similar to the input. Cheap dedupe helper.

    Filters are pushed into Cypher — the previous implementation fetched
    every finding in the repository and scanned in Python, which grew O(N)
    with the repo's audit history on every call.
    """
    if not (dedupe_key or title_substring):
        return []
    query: dict[str, object] = {
        "repository_uid": repository_uid,
        "status__in": _ACTIVE_STATUSES,
    }
    if dedupe_key:
        # Exact dedupe_key match is the primary signal; title fallback is
        # only consulted when no exact match is requested.
        query["dedupe_key"] = dedupe_key
        return list(await Finding.nodes.filter(**query))

    # neomodel's async node set exposes `field__icontains` for a case-
    # insensitive Cypher CONTAINS — no more per-row Python lowercasing.
    query["title__icontains"] = title_substring
    return list(await Finding.nodes.filter(**query))
