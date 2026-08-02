"""Will this write run collide with one already in flight? Pure.

The `files` axis (axes.py) detects path overlap in order to CO-LOCATE tickets
inside one epic — "these would collide on separate branches, so put them in one
PR". Once epics exist, nothing compared epic A's paths against epic B's, and
m0019 moved parallelism deliberately *across* epics ("parallelism belongs
across epics, not within one"). This module is the missing other half.

Kept pure and separate from dispatch for the same reason `plan_tick` is: the
collision rule is the part worth reading, and it should be testable without a
sandbox, a lock, or a database.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PathClaim:
    """An in-flight write run's claim on a set of files."""

    run_uid: str
    ticket_uid: str
    started_at: str  # ISO-8601; the total-ordering key
    paths: frozenset[str]


def overlap(a: frozenset[str], b: frozenset[str], *, limit: int = 10) -> tuple[str, ...]:
    """The shared paths, sorted and capped — the 409's evidence.

    Sorted so the message is reproducible, capped so a claim over a large
    refactor doesn't render a wall of text into an error detail.
    """
    return tuple(sorted(a & b))[:limit]


def conflicting_claim(
    claims: Iterable[PathClaim], *, paths: frozenset[str]
) -> PathClaim | None:
    """The in-flight claim this dispatch would collide with, or None.

    Iterates in `(started_at, run_uid)` order and returns the FIRST overlap, so
    a dispatch refused twice names the same blocker both times. Without a total
    order the 409 would point at a different run on each retry and the UI's
    deep-link would jump around.

    Empty `paths` never conflicts: absence of evidence is not evidence of
    overlap, and a ticket whose findings record no paths has made no prediction
    to check. Claims with empty paths are likewise never blockers.
    """
    if not paths:
        return None
    for claim in sorted(claims, key=lambda c: (c.started_at, c.run_uid)):
        if claim.paths and (claim.paths & paths):
            return claim
    return None
