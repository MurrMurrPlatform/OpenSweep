"""Findings that became tickets leave the board — status "ticketed".

Promoting a finding to a ticket used to change nothing about the finding: it
stayed `open` and sat on the board looking exactly like work nobody had
touched, so the same finding could be promoted again and again. Triage
(acknowledge / won't-fix / mark-fixed) already moved a finding out of the
default view; conversion was the one action that did not.

`ticketed` closes that gap as an ordinary status rather than a second flag, so
"processed" stays a single question — `status <> 'open'` — instead of a matrix
of state to keep consistent. `ticket_uid` rides along purely so the detail view
can link forward; `Ticket.origin_finding_uid` is still the authoritative edge.

This migration backfills findings that were already promoted before the
behaviour existed. Two deliberate guards:

- ONLY findings still `open` are moved. Someone who promoted a finding and
  then marked it fixed meant "fixed"; the backfill must not rewrite that.
- The EARLIEST ticket wins when a finding was promoted more than once, which
  matches the runtime rule (mark_ticketed no-ops on a non-open finding, so the
  first ticket is the one that gets the link). Without the ORDER BY, which
  ticket won would depend on scan order.

ORDERING NOTE: VERSION 20, follows m0019 (drop-epic-dispatch-state). No
SCHEMA_UP — Neo4j is schemaless and `ticket_uid` is a plain property.

The DOWN is two statements, not one. Reverting the status is not enough:
`ticket_uid` outlives the `ticketed` status by design (a finding promoted and
then marked fixed keeps the link until its ticket is deleted), so a DOWN that
only touched `status = 'ticketed'` nodes would strand the property on exactly
those findings. Removing the property unconditionally is safe — nothing but
this feature ever wrote it.
"""

VERSION = 20
NAME = "finding-ticketed-status"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    "MATCH (t:Ticket) WHERE t.origin_finding_uid IS NOT NULL AND t.origin_finding_uid <> '' "
    "WITH t.origin_finding_uid AS fuid, t ORDER BY t.created_at ASC "
    "WITH fuid, collect(t)[0] AS t0 "
    "MATCH (f:Finding {uid: fuid}) WHERE f.status = 'open' "
    "SET f.status = 'ticketed', f.ticket_uid = t0.uid",
]

DOWN: list[str] = [
    "MATCH (f:Finding) WHERE f.status = 'ticketed' SET f.status = 'open'",
    "MATCH (f:Finding) WHERE f.ticket_uid IS NOT NULL REMOVE f.ticket_uid",
]
