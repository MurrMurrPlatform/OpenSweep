"""RunPolicy resolution + routing checks.

PLATFORM.md §Run policies, rules 1, 3, 4:
- No Run starts without a resolved RunPolicy.
- Routing constraints are checked at queue time, not at the executor.
- Aggregate budgets cap autonomous Runs; human-triggered Runs warn-not-block.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from neomodel import adb

from domains.runs.schemas import Executor, RunTrigger
from domains.run_policies.models import RunPolicy
from domains.run_policies.services.system_default import (
    ensure_system_default,
    get_system_default,
)


@dataclass
class ResolvedPolicy:
    policy: RunPolicy
    repository_uid: str
    executor: Executor
    trigger: RunTrigger
    warnings: list[str]


class PolicyViolation(RuntimeError):
    """Raised when policy resolution refuses dispatch.

    API layers translate this to HTTP 409 (or 422 for routing).
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


_CLOUD_EXECUTORS = {Executor.CLAUDE_CODE, Executor.CODEX}


async def resolve(
    *,
    repository_uid: str,
    executor: Executor,
    trigger: RunTrigger,
    run_policy_uid: Optional[str] = None,
    default_policy_uid: Optional[str] = None,
) -> ResolvedPolicy:
    """Resolve the effective RunPolicy, then validate routing + budgets.

    Priority: explicit per-run override > per-stage workflow pin > system default.
    """
    chosen_uid = run_policy_uid or default_policy_uid
    if chosen_uid:
        policy = await RunPolicy.nodes.get_or_none(uid=chosen_uid)
        if policy is None:
            raise PolicyViolation("policy_not_found", f"RunPolicy {chosen_uid} not found")
    else:
        # Fall back to the system-default policy. Auto-create on first use so
        # a brand-new install never blocks a Run with "no_policy".
        policy = await get_system_default()
        if policy is None:
            policy = await ensure_system_default()

    # Normalize legacy rows where local_only and cloud_allowed are both true
    # (the previous schema allowed it; the new validator does not). Treat
    # local_only as authoritative — it's the stricter constraint.
    if policy.local_only and policy.cloud_allowed:
        policy.cloud_allowed = False

    warnings: list[str] = []
    _check_routing(policy, executor)
    await _check_aggregate_budgets(
        policy, repository_uid=repository_uid, trigger=trigger, warnings=warnings
    )

    return ResolvedPolicy(
        policy=policy,
        repository_uid=repository_uid,
        executor=executor,
        trigger=trigger,
        warnings=warnings,
    )


def _check_routing(policy: RunPolicy, executor: Executor) -> None:
    is_cloud = executor in _CLOUD_EXECUTORS
    if policy.local_only and is_cloud:
        raise PolicyViolation(
            "routing_local_only",
            f"executor={executor.value} blocked by RunPolicy local_only=true",
        )
    if is_cloud and not policy.cloud_allowed:
        raise PolicyViolation(
            "routing_cloud_blocked",
            f"executor={executor.value} requires cloud_allowed=true",
        )
    allow = list(policy.allowed_executors or [])
    if allow and executor.value not in allow:
        raise PolicyViolation(
            "routing_not_in_allowlist",
            f"executor={executor.value} not in allowed_executors {allow}",
        )


async def _check_aggregate_budgets(
    policy: RunPolicy,
    *,
    repository_uid: str,
    trigger: RunTrigger,
    warnings: list[str],
) -> None:
    """Hard-cap autonomous (event/schedule) Runs; warn-only on manual."""
    block = trigger != RunTrigger.MANUAL
    # Run.created_at lands in the store as a numeric epoch
    # (neomodel DateTimeProperty serializer), so we compare against an epoch
    # rather than a Cypher DateTime.
    since_epoch = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()

    if policy.daily_repo_run_count:
        rows, _ = await adb.cypher_query(
            "MATCH (r:Run) "
            "WHERE r.repository_uid = $u AND r.created_at >= $since "
            "RETURN count(r)",
            {"u": repository_uid, "since": since_epoch},
        )
        n = rows[0][0] if rows else 0
        if n >= policy.daily_repo_run_count:
            msg = f"daily_repo_run_count {policy.daily_repo_run_count} reached ({n})"
            if block:
                raise PolicyViolation("budget_run_count", msg)
            warnings.append(msg)

    if policy.daily_repo_wall_seconds:
        rows, _ = await adb.cypher_query(
            "MATCH (r:Run) "
            "WHERE r.repository_uid = $u AND r.created_at >= $since "
            "RETURN coalesce(sum(r.duration_ms), 0)",
            {"u": repository_uid, "since": since_epoch},
        )
        ms = int(rows[0][0]) if rows else 0
        secs = ms // 1000
        if secs >= policy.daily_repo_wall_seconds:
            msg = (
                f"daily_repo_wall_seconds {policy.daily_repo_wall_seconds} reached "
                f"({secs}s)"
            )
            if block:
                raise PolicyViolation("budget_wall_seconds", msg)
            warnings.append(msg)

    if policy.daily_repo_dollars:
        # Run.usage is a JSONProperty (a JSON string in the store), so the
        # dollar figures cannot be summed in Cypher — one query fetches the
        # window's usage blobs and Python sums usage["dollars"] (executor-
        # reported; 0.0 for unmetered/subscription runs).
        rows, _ = await adb.cypher_query(
            "MATCH (r:Run) "
            "WHERE r.repository_uid = $u AND r.created_at >= $since "
            "RETURN r.usage",
            {"u": repository_uid, "since": since_epoch},
        )
        total = 0.0
        for row in rows:
            raw = row[0]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    continue
            if not isinstance(raw, dict):
                continue
            try:
                total += float(raw.get("dollars") or 0.0)
            except (TypeError, ValueError):
                continue
        if total >= policy.daily_repo_dollars:
            msg = (
                f"daily_repo_dollars {policy.daily_repo_dollars} reached "
                f"(${total:.2f})"
            )
            if block:
                raise PolicyViolation("budget_dollars", msg)
            warnings.append(msg)
