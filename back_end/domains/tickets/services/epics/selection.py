"""WHICH tickets an epic may draw from — filter, sort, cap. Pure.

Step one of the two-step contract in `schemas`: `select_tickets` narrows a
loaded candidate pool to the ordered shortlist that `axes.partition` then cuts
into epics. It is deliberately the only place selection semantics live, so the
three producers (human multi-select, deterministic rule, grouping agent)
cannot each grow their own idea of "top 10 open security tickets".

Two properties matter more than anything else here:

* **`>=` ladders, not exact matches.** The findings API's `?severity=high`
  could never express "critical and high", which is what people ask for.
  The ladders come from `infrastructure.ranking` — a fourth private copy of
  the severity order is exactly what that module exists to prevent.
* **Total ordering.** The same rule must produce the same epics every time it
  runs, or re-running it silently proposes different work and nothing
  downstream is reproducible. Every sort therefore ends in `uid`, which is
  unique, so no two tickets can ever compare equal.

An unset filter admits everything. Callers pass the whole `EpicSelection`
through unconditionally, so a filter nobody configured must not quietly
shrink the pool.
"""

from __future__ import annotations

from domains.areas.models import child_key_prefix_of
from domains.tickets.services.epics.schemas import EpicSelection, TicketFacts
from infrastructure.ranking import (
    meets_min_priority,
    meets_min_severity,
    priority_rank,
    severity_rank,
)

#: Accepted `EpicSelection.sort` values. Anything else falls back to
#: `priority` rather than raising: a stored rule with a stale sort key should
#: still run, just in the default order.
SORT_FIELDS = ("priority", "severity", "age", "size")

#: Fix-size estimate ladder, mirroring `findings.models.FINDING_SIZES`. It is
#: local because importing that module drags in neomodel, and because size is
#: not a severity/priority ladder — it must not be conflated with either.
#: Belongs next to the other ladders in `infrastructure.ranking` once that
#: file is free to change.
SIZE_ORDER: dict[str, int] = {"trivial": 0, "small": 1, "medium": 2, "large": 3}

#: Unsized tickets rank as `medium`, the same convention (and for the same
#: reason) as `ranking._UNKNOWN`: an unknown size must neither jump the queue
#: nor vanish to the bottom of it.
_UNKNOWN_SIZE = 2

#: Placeholder for a missing `updated_at`. Only ever compared against another
#: placeholder — the leading flag in the sort key decides first.
_NO_STAMP = ""


def _stamp(facts: TicketFacts) -> tuple[int, object]:
    """Newest-first key. Under `reverse=True` the `(0, …)` flag drags an
    undated ticket to the bottom: "we don't know when this moved" is not a
    reason to promote it."""
    if facts.updated_at is None:
        return (0, _NO_STAMP)
    return (1, facts.updated_at)


def _age(facts: TicketFacts) -> tuple[int, object]:
    """Oldest-first key. Undated tickets sort last here too — an unknown age
    must not win the top of a list whose whole point is "the oldest"."""
    if facts.updated_at is None:
        return (1, _NO_STAMP)
    return (0, facts.updated_at)


def _matches_area(facts: TicketFacts, wanted: tuple[str, ...]) -> bool:
    """True when any of the ticket's areas is, or nests under, a wanted key.

    Ancestry goes through `child_key_prefix_of` for the reason `planner`
    documents: a bare `startswith` would make "backend-jobs" a child of
    "backend".
    """
    return any(
        key == want or child_key_prefix_of(want, key)
        for key in facts.area_keys
        for want in wanted
    )


def _admits(facts: TicketFacts, spec: EpicSelection) -> bool:
    """Every configured filter must pass; unconfigured ones abstain."""
    if spec.statuses and facts.status not in spec.statuses:
        return False
    if not meets_min_priority(facts.priority, spec.min_priority):
        return False
    if not meets_min_severity(facts.severity, spec.min_severity):
        return False
    # Labels/kinds are OR within a filter, AND across filters: "security OR
    # perf, and only defects" is the shape rules are written in.
    if spec.labels and not any(label in spec.labels for label in facts.labels):
        return False
    if spec.kinds and facts.kind not in spec.kinds:
        return False
    if spec.area_keys and not _matches_area(facts, spec.area_keys):
        return False
    return True


def _ordered(facts: list[TicketFacts], sort: str) -> list[TicketFacts]:
    """Total order over the pool, built from stable passes.

    Least-significant key first: `uid` is applied up front and every later
    pass preserves it (Python's sort is stable, and `reverse=True` keeps ties
    in their existing order rather than flipping them). That is what makes
    the tie-break composable — `priority` ties fall back to recency, recency
    ties fall back to uid, and uid is unique, so the order is total.
    """
    out = sorted(facts, key=lambda f: f.uid)
    if sort == "age":
        out.sort(key=_age)
    elif sort == "size":
        # Smallest first: grouping is how "clear the cheap stuff in one PR"
        # gets expressed, so the sort that names size means "quick wins".
        out.sort(key=lambda f: SIZE_ORDER.get(f.size, _UNKNOWN_SIZE))
    elif sort == "severity":
        out.sort(key=_stamp, reverse=True)
        out.sort(key=lambda f: severity_rank(f.severity), reverse=True)
    else:  # priority — also the fallback for an unrecognized sort key
        out.sort(key=_stamp, reverse=True)
        out.sort(key=lambda f: priority_rank(f.priority), reverse=True)
    return out


def select_tickets(facts: list[TicketFacts], spec: EpicSelection) -> list[TicketFacts]:
    """The eligible tickets, strongest first, capped at `spec.limit`.

    Filter → sort → cap, in that order: the cap is "top X of what qualifies",
    so it must land on the ordered survivors, never on the raw pool.

    Excluding already-placed tickets is NOT done here. `TicketFacts` carries
    no already-placed signal, and inventing one would put a second, weaker
    copy of that state next to the graph edge that actually holds it.
    `loader.load_ticket_facts(exclude_grouped=...)` owns it and pre-filters;
    this function trusts the pool it is handed.

    Input is never mutated. `spec.limit <= 0` means uncapped.
    """
    kept = [f for f in facts if _admits(f, spec)]
    ordered = _ordered(kept, spec.sort)
    return ordered[: spec.limit] if spec.limit > 0 else ordered
