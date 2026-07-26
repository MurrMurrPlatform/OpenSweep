"""Drop the epic dispatch bookkeeping — an epic is one run, one PR.

m0018 added `shape`, `dispatch_state`, `dispatched`, `dispatch_started_at`,
`last_error` and `max_parallel` so a background tick could fan an approved
epic out into one implement run per member, bounded by provider headroom.

That machinery turned out to be redundant. "Work the top 12 tickets in 4 runs"
is already satisfied by FOUR EPICS, each shipping as one run — which is
exactly what the planner produces. Parallelism belongs across epics, not
within one, so `parallel-runs` was a second way to express something the plan
already expressed, at the cost of a beat, a capacity composition, a retry
deadline and six columns.

Approval now passes Gate 1 on the epic parent and stops. The existing
Implement button starts the single run, on the same well-worn path every other
write in the platform uses.

The columns are dropped rather than left dormant: a field nothing reads is a
field that will be misread later.

ORDERING NOTE: VERSION 19, follows m0018 (epic-dispatch). The DOWN restores
the columns as NULL, not their old values — the tick that populated them is
gone, so there is nothing meaningful to restore them to.
"""

VERSION = 19
NAME = "drop-epic-dispatch-state"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    "MATCH (p:EpicProposal) REMOVE p.shape, p.dispatch_state, p.dispatched, "
    "p.dispatch_started_at, p.last_error, p.max_parallel",
]

DOWN: list[str] = [
    # Re-establish the shape every epic actually had; the rest stay unset.
    "MATCH (p:EpicProposal) SET p.shape = 'single-pr', p.dispatch_state = '', "
    "p.dispatched = '[]', p.last_error = '', p.max_parallel = 3",
]
