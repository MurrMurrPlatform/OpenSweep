"""Assemble `TicketFacts` — the one I/O step in epic planning.

Everything downstream (`selection.select_tickets`, `axes.partition`) is a pure
function over these facts, which is what makes epic composition testable
without a database and deterministic across runs. All the graph walking is
concentrated here: ticket → findings (for paths, kind, tags) and paths → areas
(for the area/feature axes).

Reads are batched per repository rather than per ticket. A naive loader would
issue two lookups per candidate, and selection runs over the whole open
backlog.
"""

from __future__ import annotations

import re

from domains.areas.models import Area
from domains.findings.models import Finding
from domains.repositories.services.path_matching import watches_path
from domains.tickets.models import Ticket
from domains.tickets.services.epics.schemas import TicketFacts


async def load_ticket_facts(
    repository_uid: str, *, exclude_grouped: bool = True
) -> list[TicketFacts]:
    """Build the flat, finding-enriched view of a repository's tickets.

    `exclude_grouped` drops tickets that already sit under a group parent, and
    the parents themselves. Both would otherwise be re-placed into a second
    group while the first is still in flight, and a ticket in two epics means
    two branches editing the same code.
    """
    tickets = list(await Ticket.nodes.filter(repository_uid=repository_uid))
    # Archived tickets left the board — epic planning must not pull them back.
    # getattr: nodes written before m0021 read None.
    tickets = [t for t in tickets if not getattr(t, "archived", False)]
    if exclude_grouped:
        parent_uids = {t.parent_ticket_uid for t in tickets if t.parent_ticket_uid}
        tickets = [
            t
            for t in tickets
            if not t.parent_ticket_uid and t.uid not in parent_uids
        ]
    if not tickets:
        return []

    findings = {
        f.uid: f for f in await Finding.nodes.filter(repository_uid=repository_uid)
    }
    areas = [
        a
        for a in await Area.nodes.filter(repository_uid=repository_uid)
        if a.enabled and a.kind in ("subsystem", "feature")
    ]

    out: list[TicketFacts] = []
    for t in tickets:
        # origin_finding_uid first so its facets win when a ticket links
        # several findings — it is the one the ticket was promoted from.
        uids: list[str] = []
        if t.origin_finding_uid:
            uids.append(t.origin_finding_uid)
        uids += [u for u in (t.linked_finding_uids or []) if u not in uids]
        linked = [findings[u] for u in uids if u in findings]

        paths: list[str] = []
        for f in linked:
            for raw in f.affected_paths or []:
                p = normalize_path(str(raw))
                if p and p not in paths:
                    paths.append(p)

        # Denormalized facets are authoritative (m0017 backfilled them, and
        # promotion writes them); fall back to the live finding for rows that
        # predate the backfill or whose ticket was hand-created and linked
        # afterwards.
        primary = linked[0] if linked else None
        severity = t.severity or (primary.severity if primary else "") or ""
        kind = t.kind or (primary.kind if primary else "") or ""
        subtype = t.subtype or (primary.subtype if primary else "") or ""
        tags = list(t.tags or []) or list((primary.tags if primary else []) or [])

        area_keys = _matching_keys(areas, paths, "subsystem")
        feature_keys = _matching_keys(areas, paths, "feature")

        out.append(
            TicketFacts(
                uid=t.uid,
                title=t.title or "",
                description=t.description or "",
                priority=t.priority or "medium",
                severity=severity,
                status=t.status or "backlog",
                labels=tuple(t.labels or []),
                size=t.size or "",
                paths=tuple(paths),
                kind=kind,
                tags=tuple(tags),
                subtype=subtype,
                finding_uids=tuple(uids),
                area_keys=tuple(area_keys),
                feature_keys=tuple(feature_keys),
                updated_at=t.updated_at or t.created_at,
            )
        )
    return out


#: Trailing `:12` or `:12-34` on an affected path. Agents write findings with
#: line anchors — in this repository EVERY finding carries one.
_LINE_ANCHOR = re.compile(r":\d+(?:-\d+)?$")


def normalize_path(path: str) -> str:
    """Strip a line anchor so paths compare as FILES.

    `Finding.affected_paths` entries look like
    `back_end/domains/areas/services/area_service.py:549-561`. The `files`
    axis intersects these as plain strings, so without this two tickets
    editing the same file at different lines share nothing and are never
    grouped — the axis silently fails at the one job it has, and looks like
    "no tickets happen to overlap" rather than a bug.

    Area/feature matching tolerated the suffix by accident (`watches_path`
    matches prefixes), but normalizing once here keeps every axis, and the
    evidence line, working on the same clean values.
    """
    return _LINE_ANCHOR.sub("", (path or "").strip())


def _matching_keys(areas: list, paths: list[str], kind: str) -> list[str]:
    """Area keys of `kind` whose scope covers any of `paths`.

    Deterministically ordered (sorted) so the same ticket always yields the
    same key list — the partitioner picks the most specific key from it, and
    an unstable order there would make identical input produce different
    epics on different runs.
    """
    keys = {
        a.key
        for a in areas
        if a.kind == kind
        and any(watches_path(list(a.scope_paths or []), p) for p in paths)
    }
    return sorted(keys)
