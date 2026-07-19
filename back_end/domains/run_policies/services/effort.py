"""Effort selector → seeded RunPolicy mapping (short/normal/deep/unlimited)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from domains.investigations.schemas import InvestigationEffort
from domains.run_policies.models import RunPolicy
from domains.run_policies.services.system_default import ensure_system_default

# max_wall_seconds: 0 = explicitly no wall guard (see resolve_wall_ceiling);
# None on any other ceiling = no ceiling.
_EFFORT_POLICIES: dict[InvestigationEffort, dict] = {
    InvestigationEffort.SHORT: {
        "name": "opensweep-short",
        "description": "Short run: quick, bounded checks.",
        "max_dollars": 2.0,
        "max_wall_seconds": 900,
        "max_tool_turns": 50,
        "max_files_touched": 25,
    },
    InvestigationEffort.NORMAL: {
        "name": "opensweep-normal",
        "description": "Normal run: standard investigation ceilings.",
        "max_dollars": 20.0,
        "max_wall_seconds": 3600,
        "max_tool_turns": 200,
        "max_files_touched": 100,
    },
    InvestigationEffort.DEEP: {
        "name": "opensweep-deep",
        "description": "Deep run: whole-repo audits, generous ceilings.",
        "max_dollars": 50.0,
        "max_wall_seconds": 14400,
        "max_tool_turns": 3000,
        "max_files_touched": 10000,
    },
    InvestigationEffort.UNLIMITED: {
        "name": "opensweep-unlimited",
        "description": "Unlimited run: no ceilings — stop it from the UI.",
        "max_dollars": None,
        "max_wall_seconds": 0,
        "max_tool_turns": None,
        "max_files_touched": None,
    },
}

# Old seeded rows (by name) whose values exactly match the legacy seed were
# never human-tuned: rename + roll them forward to the current tier config.
_LEGACY_POLICIES: dict[str, tuple[dict, InvestigationEffort]] = {
    "opensweep-effort-quick": (
        {"max_dollars": 0.25, "max_wall_seconds": 120, "max_tool_turns": 20, "max_files_touched": 25},
        InvestigationEffort.SHORT,
    ),
    "opensweep-effort-deep": (
        {"max_dollars": 25.0, "max_wall_seconds": 7200, "max_tool_turns": 1500, "max_files_touched": 10000},
        InvestigationEffort.DEEP,
    ),
}


async def ensure_policy_for_effort(effort: InvestigationEffort) -> RunPolicy:
    config = _EFFORT_POLICIES[effort]
    migrated = await _migrate_legacy_policy(effort)
    if migrated is not None:
        return migrated
    existing = await _find_by_name(config["name"])
    if existing is not None:
        return existing

    base = await ensure_system_default()
    policy = RunPolicy(
        uid=uuid4().hex,
        name=config["name"],
        description=config["description"],
        max_tokens=None,
        max_dollars=config["max_dollars"],
        max_wall_seconds=config["max_wall_seconds"],
        max_tool_turns=config["max_tool_turns"],
        max_files_touched=config["max_files_touched"],
        max_test_seconds=None,
        cloud_allowed=base.cloud_allowed,
        local_only=base.local_only,
        allowed_executors=list(base.allowed_executors or []),
        dry_run=base.dry_run,
        warn_at_pct=base.warn_at_pct,
        on_exceed=base.on_exceed,
        daily_repo_run_count=base.daily_repo_run_count,
        daily_repo_wall_seconds=base.daily_repo_wall_seconds,
        daily_repo_dollars=base.daily_repo_dollars,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await policy.save()
    return policy


async def _migrate_legacy_policy(effort: InvestigationEffort) -> RunPolicy | None:
    """Rename an untouched legacy effort policy in place (uid — and therefore
    every Investigation.run_policy_uid reference — is preserved)."""
    for legacy_name, (legacy_values, target) in _LEGACY_POLICIES.items():
        if target != effort:
            continue
        row = await _find_by_name(legacy_name)
        if row is None:
            continue
        untouched = all(
            getattr(row, field_name, None) == value
            for field_name, value in legacy_values.items()
        )
        if not untouched:
            return None  # human-tuned: leave it; caller seeds the new policy
        config = _EFFORT_POLICIES[effort]
        row.name = config["name"]
        row.description = config["description"]
        for field_name in ("max_dollars", "max_wall_seconds", "max_tool_turns", "max_files_touched"):
            setattr(row, field_name, config[field_name])
        row.updated_at = datetime.now(timezone.utc)
        await row.save()
        return row
    return None


async def _find_by_name(name: str) -> RunPolicy | None:
    for policy in await RunPolicy.nodes.all():
        if (policy.name or "") == name:
            return policy
    return None
