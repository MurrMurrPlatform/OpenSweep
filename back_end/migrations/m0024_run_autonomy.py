"""Autonomy dial + assumption ledger (2026-08-02).

Adds the question-policy tier — `interrogate | assume | strict` — as a property
distinct from `Effort`. Effort is the COMPUTE dial; autonomy is the ATTENTION
dial. m0010 renamed `ScheduledAgent.compute_dial` → `autonomy` to keep those
apart, and this migration deliberately does NOT touch `ScheduledAgent`: its
`autonomy` is a run-PERMISSION dial (`disabled … auto-run-any`), a different
vocabulary that happens to share the word.

Initialised to `""`, NOT to `"interrogate"`. `""` means "this row predates the
dial", which is true; writing `"interrogate"` would claim a decision nobody
made. `normalize_autonomy("")` yields INTERROGATE at read time, so behaviour is
identical without the lie — the same convention `Run.effort`'s `""` uses.

`Ticket.assumptions` and `Verdict.assumption_results` are deliberately absent
below: a missing JSONProperty reads as None and every read site uses `or []`,
so backfilling would only risk the bare-`{}`-Cypher-literal footgun that
`tests/test_migration_json_defaults.py` exists to catch.
"""

VERSION = 24
NAME = "run-autonomy"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    "MATCH (r:Run) WHERE r.autonomy IS NULL SET r.autonomy = ''",
    "MATCH (t:Thread) WHERE t.autonomy IS NULL SET t.autonomy = ''",
    "MATCH (t:Ticket) WHERE t.autonomy IS NULL SET t.autonomy = ''",
    "MATCH (r:Repository) WHERE r.default_autonomy IS NULL SET r.default_autonomy = ''",
]

DOWN: list[str] = [
    "MATCH (r:Run) REMOVE r.autonomy",
    "MATCH (t:Thread) REMOVE t.autonomy",
    "MATCH (t:Ticket) REMOVE t.autonomy",
    "MATCH (r:Repository) REMOVE r.default_autonomy",
]
