"""Ticket near-duplicate detection at create time.

Tickets have no dedupe today — a run that files eight ticket variants of the
same underlying issue produces eight tickets. Ticket 5570ff15 (semantic
dedupe via embeddings) is the target end state; this module is the minimal
lexical layer that catches the obvious "same title, same repo, still open"
duplicates without waiting on embedding infrastructure. It reuses the same
title normalisation used for findings, so a ticket refusing to file behaves
the same way a duplicate finding does today.

Callers that have reviewed the candidate and are certain the new ticket is
distinct can pass `allow_duplicate=True` on `CreateTicketRequest` to skip
the check — the same escape-hatch shape the full semantic dedupe design
calls for. Finding-promoted tickets bypass this check entirely because a
finding-backed ticket is a specific user-approved action; the dedupe that
matters there happens on the finding, not on its promotion.
"""

from __future__ import annotations

# Public helper — safe to reuse across domains (the private `_normalise_title`
# below the same file stays inside findings).
from domains.findings.services.dedupe import titles_similar


async def find_open_ticket_duplicate(
    *, repository_uid: str, title: str, threshold: float = 0.85
) -> object | None:
    """Return the first open ticket in `repository_uid` whose title reads as
    the same issue as `title`, or None.

    Threshold is intentionally stricter than findings' 0.75: ticket titles
    tend to be shorter (fewer characters, higher variance per edit), and a
    false positive here refuses a caller's write. Better to let one obvious
    duplicate through than to block a legitimately distinct ticket.

    "Open" here excludes `done` and `archived` — a closed ticket is not
    something a new one would duplicate. The list scans one repo's tickets;
    the tenant-scoped ORM filter is the perf bound.
    """
    from domains.tickets.models import Ticket

    incoming = (title or "").strip()
    if not incoming:
        return None

    candidates = await Ticket.nodes.filter(repository_uid=repository_uid)
    for c in candidates:
        if (c.status or "backlog") == "done":
            continue
        if bool(getattr(c, "archived", False)):
            continue
        if titles_similar(c.title or "", incoming, threshold=threshold):
            return c
    return None
