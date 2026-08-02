"""Platform tool: record_assumption.

The counterweight to `ask_user`. Under `autonomy=assume` the agent answers most
open questions itself; an answer it does not record is a silent guess, which is
exactly what makes autonomy dangerous. Recording turns it into a cheap, visible,
reviewable decision: the assumption reaches the PR body and the review intent,
and the reviewer's verdict lands in `Verdict.assumption_results`.

Stored on the Ticket rather than as a Thread event or a node of its own: batch
triage has no thread, per-ticket querying would otherwise mean walking every
thread's event list, and `plan`/`acceptance_criteria` already live as JSON on
the Ticket, so a node would buy a join for no new capability.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from infrastructure.audit import write_audit

#: Same triple as `Verdict.ac_results`, so the review agent emits a familiar
#: shape. `result` is the REVIEWER's judgment and is never set by the agent
#: that records the assumption.
CONFIDENCE = frozenset({"high", "medium", "low"})

_WS = re.compile(r"\s+")


def fingerprint(assumption: str) -> str:
    """Dedup key: whitespace-collapsed, case-folded assumption text.

    A resumed or continued run re-reads its own context and will re-state the
    same assumption; without this the ledger grows a duplicate every pass. Same
    convention as `Memory.fingerprint`.
    """
    return _WS.sub(" ", (assumption or "").strip().lower())


def merge_assumption(existing: list[dict], entry: dict) -> list[dict]:
    """Upsert `entry` into `existing` on its fingerprint. Pure.

    An update REPLACES the prior record rather than appending: the agent
    refining its own wording is not two assumptions. The reviewer's `result` is
    preserved across the update — re-stating an assumption must not silently
    clear a `refuted` verdict a human already acted on.
    """
    key = fingerprint(entry.get("assumption", ""))
    out = []
    replaced = False
    for row in existing or []:
        if fingerprint(row.get("assumption", "")) == key:
            merged = {**entry}
            if row.get("result") and row.get("result") != "unreviewed":
                merged["result"] = row["result"]
                merged["note"] = row.get("note", "")
            out.append(merged)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(entry)
    return out


async def record_assumption(
    *,
    ticket_uid: str,
    assumption: str,
    because: str = "",
    confidence: str = "medium",
    question: str = "",
    executor: str = "manual",
) -> dict[str, Any]:
    from domains.tickets.models import Ticket

    text = (assumption or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="assumption must be non-empty")
    ticket = await Ticket.nodes.get_or_none(uid=ticket_uid)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_uid} not found")

    entry = {
        "assumption": text[:2000],
        "because": (because or "").strip()[:2000],
        "confidence": confidence if confidence in CONFIDENCE else "medium",
        # The reviewer fills this in; the recorder never claims its own guess
        # was right.
        "result": "unreviewed",
        "note": "",
        "question": (question or "").strip()[:2000],
        "source_run_uid": executor if executor != "manual" else "",
        "ts": datetime.now(UTC).isoformat(),
    }
    ticket.assumptions = merge_assumption(list(ticket.assumptions or []), entry)
    ticket.updated_at = datetime.now(UTC)
    await ticket.save()

    await write_audit(
        kind="ticket.assumption_recorded",
        subject_uid=ticket.uid,
        subject_type="Ticket",
        actor_uid=executor,
        repository_uid=ticket.repository_uid or "",
        payload={"assumption": entry["assumption"], "confidence": entry["confidence"]},
    )
    return {
        "ticket_uid": ticket.uid,
        "recorded": entry["assumption"],
        "total": len(ticket.assumptions or []),
    }
