"""Staleness-driven audit target selection (§F) — the thin layer over
sweep.run_audit.

Whole-repo review coverage as an emergent property: rank Doc pages by how
badly they need a look (never checked > code changed since the last check),
take the top N, and let run_audit fan out one scoped audit run per page.
Selection is pure path/timestamp math — no LLM involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domains.checked.models import Checked
from domains.docs.models import Doc

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PageInfo:
    doc_uid: str
    slug: str
    has_body: bool
    created_at: datetime | None
    code_changed_at: datetime | None
    last_checked: datetime | None  # latest non-failed Checked stamp


@dataclass(frozen=True)
class AuditTarget:
    doc_uid: str
    slug: str
    reason: str  # never-checked | code-changed-since-check
    last_checked: datetime | None
    code_changed_at: datetime | None


def rank_targets(pages: list[PageInfo], *, limit: int) -> list[AuditTarget]:
    """Pure ranking: never-checked pages first (oldest first — they have
    waited longest), then pages whose code moved after their last check
    (stalest check first). Fresh pages and empty shells are not targets."""
    never, stale = [], []
    for p in pages:
        if not p.has_body:
            continue
        if p.last_checked is None:
            never.append(p)
        elif p.code_changed_at is not None and p.code_changed_at > p.last_checked:
            stale.append(p)
    never.sort(key=lambda p: p.created_at or _EPOCH)
    stale.sort(key=lambda p: p.last_checked or _EPOCH)

    out = [
        AuditTarget(p.doc_uid, p.slug, "never-checked", p.last_checked, p.code_changed_at)
        for p in never
    ] + [
        AuditTarget(
            p.doc_uid, p.slug, "code-changed-since-check", p.last_checked, p.code_changed_at
        )
        for p in stale
    ]
    return out[: max(limit, 0)]


async def select_audit_targets(repository_uid: str, *, limit: int = 3) -> list[AuditTarget]:
    """Load Docs + their latest completed Checked stamp and rank. Failed
    stamps do not count as a check (that look never finished); docs already
    targeted by an in-flight run are excluded (no double-dispatch)."""
    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
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
            last_checked=latest.get(d.uid),
        )
        for d in docs
        if d.uid not in in_flight
    ]
    return rank_targets(pages, limit=limit)
