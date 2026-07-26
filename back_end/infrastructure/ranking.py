"""The severity and priority ladders, in one place.

Three copies of these orderings had drifted apart across the codebase
(`findings.services.finding_service._SEVERITY_RANK`,
`delivery.services.convergence.SEVERITY_ORDER`,
`tickets.services.ticket_service._PRIORITY_RANK`). They agreed on the values,
but nothing made them agree — and the `>=` selection filters added for
batching would have made a fourth. Everything ranks through here now.

Note the two ladders are NOT the same vocabulary and must not be conflated:
severity tops out at `critical` (a Finding: how bad is the thing we found),
priority tops out at `urgent` (a Ticket: how soon do we want it fixed). A
ticket promoted from a finding carries both — see `Ticket.severity`.
"""

from __future__ import annotations

SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}
PRIORITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "urgent": 3}

#: Unrecognized input ranks as `medium`. Every caller that inherited this
#: default did so deliberately: an unknown severity must not silently sort
#: to the bottom (hiding it) nor to the top (crying wolf).
_UNKNOWN = 1


def severity_rank(severity: str | None) -> int:
    return SEVERITY_ORDER.get(severity or "", _UNKNOWN)


def priority_rank(priority: str | None) -> int:
    return PRIORITY_ORDER.get(priority or "", _UNKNOWN)


def meets_min_severity(severity: str | None, minimum: str | None) -> bool:
    """`severity >= minimum` on the ladder. An empty/unknown `minimum` means
    "no floor configured" and admits everything — callers pass the filter
    through unconditionally, so an unset filter must not exclude anything.
    """
    if not minimum or minimum not in SEVERITY_ORDER:
        return True
    return severity_rank(severity) >= SEVERITY_ORDER[minimum]


def meets_min_priority(priority: str | None, minimum: str | None) -> bool:
    """`priority >= minimum` on the ladder. Unset admits everything."""
    if not minimum or minimum not in PRIORITY_ORDER:
        return True
    return priority_rank(priority) >= PRIORITY_ORDER[minimum]
