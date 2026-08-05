"""Staleness-driven audit target selection (§F) — the thin layer over
sweep.run_audit.

Whole-repo review coverage as an emergent property: rank Doc pages by how
badly they need a look (never audited > stale), take the top N, and let
run_audit fan out one scoped audit run per page. Selection is pure
path/timestamp math — no LLM involved.

Two target categories, drawn from two axes so the auto-audit loop agrees
with the stale badge:
- never-audited — no completed Checked stamp yet (audit-coverage axis).
- stale — code moved under the page since it was last REVIEWED
  (code_changed_at > last_reviewed_at, the single derived staleness axis
  that Doc.stale / the UI badge use).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domains.checked.models import Checked
from domains.docs.models import Doc
from domains.freshness import is_stale

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PageInfo:
    doc_uid: str
    slug: str
    has_body: bool
    created_at: datetime | None
    code_changed_at: datetime | None
    last_reviewed_at: datetime | None  # the review axis (Doc.stale)
    last_checked: datetime | None  # latest non-failed Checked stamp (coverage)


@dataclass(frozen=True)
class AuditTarget:
    doc_uid: str
    slug: str
    reason: str  # never-checked | stale
    last_checked: datetime | None
    code_changed_at: datetime | None


def rank_targets(pages: list[PageInfo], *, limit: int) -> list[AuditTarget]:
    """Pure ranking: never-audited pages first (oldest first — they have
    waited longest), then STALE pages — code moved after their last REVIEW
    (stalest review first), matching Doc.stale / the UI badge. Fresh pages and
    empty shells are not targets."""
    never, stale = [], []
    for p in pages:
        if not p.has_body:
            continue
        if p.last_checked is None:
            never.append(p)
        elif is_stale(p.code_changed_at, p.last_reviewed_at):
            stale.append(p)
    never.sort(key=lambda p: p.created_at or _EPOCH)
    stale.sort(key=lambda p: p.last_reviewed_at or _EPOCH)

    out = [
        AuditTarget(p.doc_uid, p.slug, "never-checked", p.last_checked, p.code_changed_at)
        for p in never
    ] + [
        AuditTarget(p.doc_uid, p.slug, "stale", p.last_checked, p.code_changed_at)
        for p in stale
    ]
    return out[: max(limit, 0)]


async def select_audit_targets(repository_uid: str, *, limit: int = 3) -> list[AuditTarget]:
    """Load Docs + their latest completed Checked stamp and rank. Failed
    stamps do not count as a check (that look never finished); docs already
    targeted by an in-flight run are excluded (no double-dispatch)."""
    docs = list(await Doc.nodes.filter(repository_uid=repository_uid, archived=False))
    if not docs:
        return []

    latest: dict[str, datetime] = {}
    for c in await Checked.nodes.filter(repository_uid=repository_uid):
        if (c.outcome or "") == "failed" or not c.checked_at:
            continue
        prev = latest.get(c.scope_uid or "")
        if prev is None or c.checked_at > prev:
            latest[c.scope_uid or ""] = c.checked_at

    in_flight: set[str] = set()
    from domains.runs.services.active_runs import active_runs_for

    for run in await active_runs_for(repository_uid=repository_uid):
        target = dict(run.target or {})
        raw = target.get("doc_uids") or []
        if isinstance(raw, str):
            raw = [raw]
        in_flight.update(str(u) for u in raw)

    pages = [
        PageInfo(
            doc_uid=d.uid,
            slug=d.slug or "",
            has_body=bool((d.body or "").strip()),
            created_at=d.created_at,
            code_changed_at=d.code_changed_at,
            last_reviewed_at=d.last_reviewed_at,
            last_checked=latest.get(d.uid),
        )
        for d in docs
        if d.uid not in in_flight
    ]
    return rank_targets(pages, limit=limit)


def path_recency(stamps: list[dict]) -> dict[str, datetime]:
    """Per-path coverage recency: the latest non-failed stamp covering each
    EXACT path. Pure — stamps are {covered_paths, checked_at, outcome} dicts
    (a failed look never counts as coverage)."""
    out: dict[str, datetime] = {}
    for stamp in stamps:
        if (stamp.get("outcome") or "") == "failed":
            continue
        checked_at = stamp.get("checked_at")
        if not checked_at:
            continue
        for raw in stamp.get("covered_paths") or []:
            path = str(raw)
            prev = out.get(path)
            if prev is None or checked_at > prev:
                out[path] = checked_at
    return out


async def coverage_recency_for(repository_uid: str) -> dict[str, datetime]:
    """path_recency over the repository's Checked stamps — campaign rotation
    planning ranks areas by their least-recently-covered scope path.

    Rotation deliberately does NOT discount stamps whose `coverage_source` is
    "inferred"/"unknown", even though those paths are where a run was aimed
    rather than what it read. Two reasons, both load-bearing:

    1. `coverage_source` does not track the dispatch path. Campaign parts
       already carry `_REPORTING_CONTRACT` (campaigns/services/part_dispatch.py)
       asking for covered_paths, so the label records whether the MODEL complied
       — ranking on it would rank on prompt compliance.
    2. Any absolute penalty breaks round-robin. Recency here is the only memory
       rotation has of what it already dispatched, so aging a stamp by a
       constant pins an inferred-only area's apparent age at that constant no
       matter how often it is audited: auditing it never retires it from the
       queue. Simulated over weekly ticks, a 30-day penalty starved the areas
       whose agents DID report by ~4x — an incentive to report less.

    Honesty about coverage belongs where a human reads it (the areas board and
    coverage strip badge non-reported stamps "scope only"), not in this ranker.
    """
    stamps = [
        {
            "covered_paths": list(c.covered_paths or []),
            "checked_at": c.checked_at,
            "outcome": c.outcome or "",
        }
        for c in await Checked.nodes.filter(repository_uid=repository_uid)
    ]
    return path_recency(stamps)
