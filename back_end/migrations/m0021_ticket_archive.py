"""Ticket archive — reversible "leave the board" without losing history.

Delete is deliberately backlog-only (an approved ticket is an audit record),
which left no way to clear finished or abandoned work off the board. `archived`
is the docs-domain pattern applied to tickets: a boolean the default listings
filter on, plus `archived_at`/`archived_by` provenance mirroring the Gate-1
`approved_by`/`approved_at` pair.

The index is created here because nothing in this codebase runs neomodel's
install_labels — a model-level `index=True` alone never materializes.

The UP backfills `archived = false` so the property is non-null everywhere;
service-side filters still treat a missing value as not-archived for nodes
created in the window between deploy and migration.

ORDERING NOTE: VERSION 21, follows m0020 (finding-ticketed-status).
"""

VERSION = 21
NAME = "ticket-archive"

SCHEMA_UP: list[str] = [
    "CREATE INDEX ticket_archived IF NOT EXISTS FOR (n:Ticket) ON (n.archived)",
]
SCHEMA_DOWN: list[str] = [
    "DROP INDEX ticket_archived IF EXISTS",
]

UP: list[str] = [
    "MATCH (t:Ticket) WHERE t.archived IS NULL SET t.archived = false",
]

DOWN: list[str] = [
    "MATCH (t:Ticket) REMOVE t.archived, t.archived_at, t.archived_by",
]
