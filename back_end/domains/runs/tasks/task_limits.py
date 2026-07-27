"""Celery time limits for the per-run tasks, derived from the run's policy.

A per-run task (`dispatch_run`, `resume_run`) hosts the whole pipeline —
sandbox clone, code-graph index, every agent pass, finalize. Its Celery
limit therefore has to sit ABOVE the wall ceiling the executor is enforcing
on the agent, or the worker kills the task while the adapter still believes
it has budget: the run dies with no wind-down, no ceiling warning, and a
`SoftTimeLimitExceeded` instead of the LIMIT_EXCEEDED status the UI resumes
from.

The flat 3600/3900 these tasks used to carry was below the `deep` tier's
own 14400s ceiling (`run_policies/services/effort.py`), so worker-dispatched
deep runs could not finish — and `unlimited` (max_wall_seconds=0, "stop it
from the UI") was capped at 65 minutes without saying so.
"""

from __future__ import annotations

#: Pipeline work that happens OUTSIDE the agent's wall budget and therefore
#: has to fit between the agent ceiling and the Celery limit: shallow clone
#: plus per-ref fetches, `index_code_graph` (120s timeout of its own), static
#: analysis, finalize/persist, and the reserved wind-down pass.
DISPATCH_OVERHEAD_SECONDS = 900

#: Backstop for runs whose wall guard is disabled — the `unlimited` tier
#: (max_wall_seconds=0) and any run on a local provider, where
#: `resolve_wall_ceiling` returns None. Celery needs a finite number; this is
#: high enough not to truncate legitimate work and low enough that a wedged
#: run eventually releases its worker slot.
MAX_DISPATCH_SECONDS = 24 * 3600

#: Used when a run has no resolvable policy. Matches the previous flat value
#: so behaviour is unchanged for runs that never had a ceiling to exceed.
DEFAULT_DISPATCH_SECONDS = 3600

#: Hard limit sits this far above the soft limit: SoftTimeLimitExceeded is
#: raised first so the pipeline can mark the run failed and clean up before
#: the worker kills the process outright.
_HARD_LIMIT_GRACE = 300


def dispatch_time_limits(max_wall_seconds: int | None) -> tuple[int, int]:
    """`(soft_time_limit, time_limit)` for a run whose policy wall ceiling is
    `max_wall_seconds`.

    Mirrors `executors._shared.resolve_wall_ceiling`'s reading of the field:
    a positive value is a real ceiling, ``0`` means explicitly no wall guard,
    and ``None`` means unset. Unset falls back to the default rather than the
    backstop — an unset ceiling is not a request for a 24-hour run.
    """
    if max_wall_seconds is None:
        soft = DEFAULT_DISPATCH_SECONDS + DISPATCH_OVERHEAD_SECONDS
    elif int(max_wall_seconds) <= 0:
        soft = MAX_DISPATCH_SECONDS
    else:
        soft = int(max_wall_seconds) + DISPATCH_OVERHEAD_SECONDS
    soft = min(soft, MAX_DISPATCH_SECONDS)
    return soft, soft + _HARD_LIMIT_GRACE


async def limits_for_run(run) -> tuple[int, int]:
    """`dispatch_time_limits` for a Run row, resolved through its policy.

    Falls back to the default when the run pins no policy or the row is gone
    — a missing policy must not silently buy a 24-hour slot.
    """
    from domains.run_policies.models import RunPolicy

    policy_uid = (getattr(run, "run_policy_uid", "") or "").strip()
    if not policy_uid:
        return dispatch_time_limits(None)
    policy = await RunPolicy.nodes.get_or_none(uid=policy_uid)
    if policy is None:
        return dispatch_time_limits(None)
    return dispatch_time_limits(policy.max_wall_seconds)
