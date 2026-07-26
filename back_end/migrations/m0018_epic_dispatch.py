"""Epic dispatch state, and the TicketGroupProposal → EpicProposal relabel.

Adds `dispatch_state` / `dispatched` / `dispatch_started_at` / `last_error` /
`max_parallel` to the proposal so the epic tick can fan an approved epic out
into implement runs under provider headroom, and relabels the node to match
the vocabulary the product actually speaks: a parent Ticket with subtickets
IS an epic, so that is what it is called.

DELIBERATELY NOT BACKFILLED TO `pending`. Approving an epic now queues it for
dispatch, but rows approved BEFORE this landed were approved under the old
contract, where approval only created a parent ticket and a human pressed
Implement separately. Backfilling those to `pending` would have the first tick
after deploy fan out write runs for every epic ever approved in the
repository's history. They are set to `''`, which the tick ignores.

NOTE the quotes on the JSON defaults. `dispatched` and `evidence` are neomodel
JSONProperty columns, which store a JSON *string*. A bare `{}` is a Cypher MAP
literal and Neo4j rejects it outright ("Property values can only be of
primitive types or arrays thereof"), taking the migration — and therefore boot
— down with it. A bare `[]` is accepted as a native array but then fails to
inflate on read. `tests/test_migration_json_defaults.py` guards this.

ORDERING NOTE: VERSION 18, assumes m0016 (provider max concurrent runs) and
m0017 (ticket finding facets) land first — `migration_runner` requires
contiguous versions and refuses to boot on a gap.
"""

VERSION = 18
NAME = "epic-dispatch"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    # Relabel FIRST so every backfill below writes to the node the code now
    # looks for. Idempotent: a second run matches nothing.
    "MATCH (p:TicketGroupProposal) SET p:EpicProposal REMOVE p:TicketGroupProposal",
    "MATCH (p:EpicProposal) WHERE p.dispatch_state IS NULL SET p.dispatch_state = ''",
    "MATCH (p:EpicProposal) WHERE p.dispatched IS NULL SET p.dispatched = '[]'",
    "MATCH (p:EpicProposal) WHERE p.max_parallel IS NULL SET p.max_parallel = 3",
    "MATCH (p:EpicProposal) WHERE p.last_error IS NULL SET p.last_error = ''",
    # `dispatch_started_at` is deliberately left NULL on existing rows.
    # `dispatch_deadline_passed` reads NULL as "not expired", which is the safe
    # reading: these rows are not being ticked anyway (empty dispatch_state),
    # and inventing a start time could expire an epic the moment a tick sees it.
    "MATCH (p:EpicProposal) WHERE p.axis IS NULL SET p.axis = 'root-cause'",
    "MATCH (p:EpicProposal) WHERE p.evidence IS NULL SET p.evidence = '{}'",
    "MATCH (p:EpicProposal) WHERE p.shape IS NULL SET p.shape = 'single-pr'",
    "MATCH (p:EpicProposal) WHERE p.plan_uid IS NULL SET p.plan_uid = ''",
    "MATCH (p:EpicProposal) WHERE p.origin IS NULL SET p.origin = 'agent'",
]

DOWN: list[str] = [
    "MATCH (p:EpicProposal) REMOVE p.dispatch_state, p.dispatched, "
    "p.max_parallel, p.dispatch_started_at, p.last_error",
    "MATCH (p:EpicProposal) SET p:TicketGroupProposal REMOVE p:EpicProposal",
]
