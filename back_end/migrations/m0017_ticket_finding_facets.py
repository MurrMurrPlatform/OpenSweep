"""Denormalize the finding facets a Ticket is batched by.

Adds `severity`, `kind`, `tags`, `subtype` to Ticket, copied from the origin
Finding at promotion time. These are the axes batch selection filters and
partitions on:

- `severity` answers "every critical and high", which `priority` cannot —
  they are different ladders (severity tops out at `critical`, priority at
  `urgent`) and a finding's badness is not its schedule.
- `kind` is the `lens` axis; `tags` + `subtype` form the `class` (ratchet)
  axis.

Denormalized rather than joined: every batch selection would otherwise fan
out one Finding lookup per candidate ticket on a list-sized query.

Backfill walks `origin_finding_uid` — the promotion link — and leaves
human-authored tickets (which have no origin finding) with the empty
defaults. Empty severity is meaningful here: it means "not from a finding",
and `meets_min_severity` ranks it as `medium` rather than excluding it, so a
`min_severity` filter never silently swallows hand-written tickets.

ORDERING NOTE: this is VERSION 17 and assumes m0016 (provider max concurrent
runs) lands first — `migration_runner` requires contiguous versions and will
refuse to boot on a gap. m0016 was uncommitted when this was written; if it
is dropped rather than committed, renumber this to 16 before merging.
"""

VERSION = 17
NAME = "ticket-finding-facets"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    # Defaults first, so every row reads a concrete value rather than NULL.
    "MATCH (t:Ticket) WHERE t.severity IS NULL SET t.severity = ''",
    "MATCH (t:Ticket) WHERE t.kind IS NULL SET t.kind = ''",
    "MATCH (t:Ticket) WHERE t.subtype IS NULL SET t.subtype = ''",
    # NOTE the quotes: `tags` is a neomodel JSONProperty, which stores a JSON
    # *string*, not a native list. A bare `[]` would write a Neo4j array that
    # `JSONProperty.inflate` then fails to parse.
    "MATCH (t:Ticket) WHERE t.tags IS NULL SET t.tags = '[]'",
    # Then copy the facets across the promotion link. Findings are matched by
    # uid, so a ticket whose origin finding was deleted keeps the defaults.
    "MATCH (t:Ticket) WHERE t.origin_finding_uid <> '' "
    "MATCH (f:Finding {uid: t.origin_finding_uid}) "
    "SET t.severity = coalesce(f.severity, ''), "
    "    t.kind = coalesce(f.kind, ''), "
    "    t.tags = coalesce(f.tags, []), "
    "    t.subtype = coalesce(f.subtype, '')",
]

DOWN: list[str] = [
    "MATCH (t:Ticket) REMOVE t.severity, t.kind, t.tags, t.subtype",
]
