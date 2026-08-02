"""Checked.coverage_source — separate what a run READ from what it was POINTED at.

`_coverage_fields` falls back to the run's dispatched target paths when the
agent reports no coverage contract, so a run that examined one page of a
35-page scope still landed the whole scope in `covered_paths`. The fallback
stays (dropping it would blank the area coverage strip for every run that
omits the contract), but stamps now record which of the two they are.

The index is created here because nothing in this codebase runs neomodel's
install_labels — a model-level `index=True` alone never materializes.

The UP backfills "unknown" rather than "reported": stamps written before this
field existed cannot be classified after the fact, and calling them "reported"
would make the entire backlog assert evidence it never had. Readers must treat
"unknown" as "no better than inferred".

ORDERING NOTE: VERSION 22, follows m0021 (ticket-archive).
"""

VERSION = 22
NAME = "checked-coverage-source"

SCHEMA_UP: list[str] = [
    "CREATE INDEX checked_coverage_source IF NOT EXISTS "
    "FOR (n:Checked) ON (n.coverage_source)",
]
SCHEMA_DOWN: list[str] = [
    "DROP INDEX checked_coverage_source IF EXISTS",
]

UP: list[str] = [
    "MATCH (c:Checked) WHERE c.coverage_source IS NULL "
    "SET c.coverage_source = 'unknown'",
]

DOWN: list[str] = [
    "MATCH (c:Checked) REMOVE c.coverage_source",
]
