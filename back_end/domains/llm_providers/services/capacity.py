"""How many more runs a provider may take right now.

`LLMProvider.max_concurrent_runs` is the provider's own capacity ceiling —
independent of, and composed with, any one campaign's `max_parallel`. The
campaign tick clamps its dispatch capacity to the headroom computed here, so
several campaigns pointed at one subscription cannot collectively stampede
it just because each is individually under its own limit.

This bounds runs we *start*. It is not a mutual-exclusion primitive: the
codex-subscription credential lease (`codex_credential`) is the hard one, and
it serializes to 1 per subscription on the `exec` path regardless of what is
configured here. Setting a codex provider above 1 does not buy parallelism —
it buys `paused_quota` parks on the 10-minute resume beat.
"""

from __future__ import annotations

from domains.llm_providers.schemas import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    default_max_concurrent_runs,
)
from domains.runs.models import Run
from domains.runs.services.active_runs import ACTIVE_RUN_STATUSES


def configured_ceiling(provider) -> int:
    """The provider's ceiling.

    Unset (NULL/0 — a row written before m0016, or outside create/update)
    falls back to the KIND's default, not the platform's: guessing 5 for a
    codex subscription that the credential lease serializes to 1 would be
    the one wrong direction to guess in. Negative is nonsense; clamp to 1
    rather than letting it read as "unlimited" downstream.
    """
    configured = int(getattr(provider, "max_concurrent_runs", None) or 0)
    if configured <= 0:
        return default_max_concurrent_runs(getattr(provider, "kind", "") or "")
    return configured


async def active_run_count(provider_uid: str) -> int:
    """Runs currently occupying this provider.

    ACTIVE_RUN_STATUSES (queued/running/paused_quota) is the same definition
    the dispatch guard uses — `paused_quota` counts because the resume beat
    re-dispatches that very run, so it still owns a slot.
    """
    if not provider_uid:
        return 0
    rows = await Run.nodes.filter(provider_uid=provider_uid)
    return sum(1 for r in rows if (r.status or "") in ACTIVE_RUN_STATUSES)


async def provider_headroom(provider) -> int:
    """How many more runs may be started on `provider` (never negative).

    Runs already in flight for the calling campaign are included in the
    count, which is what makes this compose with the campaign's own capacity
    rather than double-count against it.
    """
    if provider is None:
        return DEFAULT_MAX_CONCURRENT_RUNS
    used = await active_run_count(getattr(provider, "uid", "") or "")
    return max(configured_ceiling(provider) - used, 0)
