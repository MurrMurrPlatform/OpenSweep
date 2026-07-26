"""Per-provider concurrency ceiling on LLMProvider.

Adds `max_concurrent_runs` — how many runs may execute on one provider at
once. Before this, nothing bounded a provider's fan-out: campaign dispatch
was capped only by `Campaign.max_parallel` (and, for codex subscriptions,
serialized to 1 by the credential lease). Existing rows are backfilled to
the model default so the DTO and the campaign tick's headroom check read a
concrete value rather than NULL.

Codex-subscription rows are backfilled to 1 instead: on the `exec` path
(every Celery-worker run — see codex_cli.app_server_enabled) the credential
lease already serializes that subscription to one run, and starting more
only parks the losers in `paused_quota` for the 10-minute resume beat.
"""

VERSION = 16
NAME = "provider-max-concurrent-runs"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    "MATCH (p:LLMProvider) WHERE p.max_concurrent_runs IS NULL "
    "SET p.max_concurrent_runs = 5",
    "MATCH (p:LLMProvider) WHERE p.kind = 'codex_subscription' "
    "SET p.max_concurrent_runs = 1",
]
DOWN: list[str] = [
    "MATCH (p:LLMProvider) REMOVE p.max_concurrent_runs",
]
