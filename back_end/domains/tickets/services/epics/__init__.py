"""Ticket epic planning — the pure decision core.

`select_tickets` (WHICH tickets) then `partition` (HOW they are cut), both
pure functions over the `schemas` contract. Every producer of an epic goes
through these two calls, so the human, the rule, the agent and the schedule
cannot drift into different notions of what an epic is. All I/O — resolving
findings into `TicketFacts`, persisting `EpicDraft`s — lives in the caller.
"""

from __future__ import annotations

from domains.tickets.services.epics.axes import partition
from domains.tickets.services.epics.schemas import (
    DETERMINISTIC_AXES,
    MAX_RATIONALE_CHARS,
    EpicAxis,
    EpicDraft,
    EpicPartition,
    EpicSelection,
    TicketFacts,
)
from domains.tickets.services.epics.selection import select_tickets

__all__ = [
    "DETERMINISTIC_AXES",
    "MAX_RATIONALE_CHARS",
    "EpicAxis",
    "EpicDraft",
    "EpicPartition",
    "EpicSelection",
    "TicketFacts",
    "partition",
    "select_tickets",
]
