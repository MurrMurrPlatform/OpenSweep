"""System-default RunPolicy -- the policy used when nothing else is set.

PLATFORM.md §Run policies, rule 1: "No Run starts without a resolved
RunPolicy." Without a system-default, every brand-new Investigation
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
DEFAULT_MAX_WALL_SECONDS = 3600  # fallback for policies with an UNSET wall only
# Earlier seeded defaults (300s, then 600s, then 3600s). A policy still sitting
# on one of these was never human-tuned, so the upsert migrates it forward; any
# other value is preserved.
LEGACY_DEFAULT_MAX_WALL_SECONDS = (300, 600, 3600)
# Earlier seeded cost/operational ceilings that proved too tight for a real
# write-path implement run: a finished, committed run that crossed them was
# tagged limit_exceeded and never pushed (no branch, no draft PR). A policy
# still sitting on the exact old seeded value was never human-tuned, so the
# upsert migrates it forward to the current _DEFAULTS; any other value is
# preserved. Keyed by field name → the exact legacy values to migrate off.
LEGACY_DEFAULT_CEILINGS = {
    "max_dollars": (1.0, 3.0, 20.0),
    "max_tool_turns": (40, 200),
    "max_files_touched": (50, 100),
}


_DEFAULTS = {
    "description": "OpenSweep default -- unlimited; stop runs from the UI, or pick a bounded policy.",
    "max_tokens": None,
    "max_dollars": None,
    # 0 = explicitly no wall guard (None would fall back to DEFAULT_MAX_WALL_SECONDS).
    "max_wall_seconds": 0,
    "max_tool_turns": None,
    "max_files_touched": None,
    "max_test_seconds": None,
    "cloud_allowed": True,
    "local_only": False,
    "allowed_executors": [],
    "dry_run": False,
    "warn_at_pct": 80,
    "on_exceed": "abort",
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
        if (
            existing.max_wall_seconds is not None
            and int(existing.max_wall_seconds) in LEGACY_DEFAULT_MAX_WALL_SECONDS
        ):
            existing.max_wall_seconds = _DEFAULTS["max_wall_seconds"]
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
