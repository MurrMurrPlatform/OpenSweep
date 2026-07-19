"""System-default RunPolicy -- the policy used when nothing else is set.

PLATFORM.md §Run policies, rule 1: "No Run starts without a resolved
RunPolicy." Without a system-default, every brand-new agent run
created through the UI is unrunnable until the user discovers the
Admin panel. That's a terrible v1 UX. This module supplies a sensible
v1 default and an idempotent upsert.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from domains.run_policies.models import RunPolicy


SYSTEM_DEFAULT_NAME = "opensweep-default"
DEFAULT_MAX_WALL_SECONDS = 3600
# Earlier seeded defaults (300s, then 600s). A policy still sitting on one of
# these was never human-tuned, so the upsert migrates it forward; any other
# value is preserved.
LEGACY_DEFAULT_MAX_WALL_SECONDS = (300, 600)
# Earlier seeded cost/operational ceilings that proved too tight for a real
# write-path implement run: a finished, committed run that crossed them was
# tagged limit_exceeded and never pushed (no branch, no draft PR). A policy
# still sitting on the exact old seeded value was never human-tuned, so the
# upsert migrates it forward to the current _DEFAULTS; any other value is
# preserved. Keyed by field name → the exact legacy values to migrate off.
LEGACY_DEFAULT_CEILINGS = {
    "max_dollars": (1.0, 3.0),
    "max_tool_turns": (40,),
    "max_files_touched": (50,),
}


_DEFAULTS = {
    "description": "OpenSweep v1 default -- sensible ceilings, cloud allowed, $20/run cap.",
    # Cost ceilings
    "max_tokens": None,
    "max_dollars": 20.0,
    # Operational ceilings. Sized for a real write-path implement run: it
    # reads, edits, runs tests, and does doc/memory upkeep, which routinely
    # exceeds 40 tool turns. Too-tight ceilings silently convert finished,
    # committed work into an unpushed limit_exceeded run (no branch push, no
    # draft PR) -- see delivery.finalize_write_run, which only pushes an
    # awaiting_input run.
    "max_wall_seconds": DEFAULT_MAX_WALL_SECONDS,
    "max_tool_turns": 200,
    "max_files_touched": 100,
    "max_test_seconds": None,
    # Routing — cloud allowed (claude_code), no local_only constraint
    "cloud_allowed": True,
    "local_only": False,
    "allowed_executors": [],
    # Behavior
    "dry_run": False,
    "warn_at_pct": 80,
    "on_exceed": "abort",
    # Aggregate budgets (per repo, rolling 24h) — start unlimited
    "daily_repo_run_count": None,
    "daily_repo_wall_seconds": None,
    "daily_repo_dollars": None,
}


async def ensure_system_default() -> RunPolicy:
    """Idempotent upsert of the system-default policy.

    If a policy with name=SYSTEM_DEFAULT_NAME exists, refresh missing
    fields but never overwrite human-tuned values. If it doesn't,
    create one.
    """
    existing = await _find_by_name(SYSTEM_DEFAULT_NAME)
    if existing is not None:
        # Backfill anything that's None and migrate the original 300s default.
        # Preserve other human-tuned values.
        dirty = False
        for k, v in _DEFAULTS.items():
            if getattr(existing, k, None) is None and v is not None:
                setattr(existing, k, v)
                dirty = True
        if int(existing.max_wall_seconds or 0) in LEGACY_DEFAULT_MAX_WALL_SECONDS:
            existing.max_wall_seconds = DEFAULT_MAX_WALL_SECONDS
            dirty = True
        for field, legacy_values in LEGACY_DEFAULT_CEILINGS.items():
            if getattr(existing, field, None) in legacy_values:
                setattr(existing, field, _DEFAULTS[field])
                dirty = True
        if dirty:
            existing.updated_at = datetime.now(timezone.utc)
            await existing.save()
        return existing

    p = RunPolicy(
        uid=uuid4().hex,
        name=SYSTEM_DEFAULT_NAME,
        **_DEFAULTS,
    )
    await p.save()
    return p


async def get_system_default() -> Optional[RunPolicy]:
    return await _find_by_name(SYSTEM_DEFAULT_NAME)


async def _find_by_name(name: str) -> Optional[RunPolicy]:
    for p in await RunPolicy.nodes.all():
        if (p.name or "") == name:
            return p
    return None
