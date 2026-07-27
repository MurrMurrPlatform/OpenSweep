"""Per-run Celery limits must never sit below the wall ceiling the executor
enforces on the agent.

Regression: the per-run tasks carried a flat soft/hard 3600/3900, while the
`deep` effort tier allows max_wall_seconds=14400 and `unlimited` allows no
wall guard at all. A worker-dispatched deep run was killed by Celery at 65
minutes while the adapter still believed it had budget — no wind-down, no
LIMIT_EXCEEDED status, just SoftTimeLimitExceeded.
"""

import pytest

from domains.run_policies.services.effort import _EFFORT_POLICIES
from domains.runs.schemas import Effort
from domains.runs.tasks.task_limits import (
    DISPATCH_OVERHEAD_SECONDS,
    MAX_DISPATCH_SECONDS,
    dispatch_time_limits,
)


def test_hard_limit_always_exceeds_soft():
    for wall in (None, 0, 900, 3600, 14400, 10**9):
        soft, hard = dispatch_time_limits(wall)
        assert hard > soft, wall


def test_positive_ceiling_gets_pipeline_overhead():
    soft, _ = dispatch_time_limits(3600)
    assert soft == 3600 + DISPATCH_OVERHEAD_SECONDS


def test_zero_means_no_wall_guard_so_backstop_applies():
    """0 = explicitly unlimited (resolve_wall_ceiling returns None)."""
    soft, _ = dispatch_time_limits(0)
    assert soft == MAX_DISPATCH_SECONDS


def test_unset_falls_back_to_default_not_backstop():
    """An unset ceiling is not a request for a 24-hour worker slot."""
    soft, _ = dispatch_time_limits(None)
    assert soft < MAX_DISPATCH_SECONDS


def test_absurd_ceiling_is_capped():
    soft, hard = dispatch_time_limits(10**9)
    assert soft == MAX_DISPATCH_SECONDS
    assert hard > MAX_DISPATCH_SECONDS  # grace still applies above the cap


@pytest.mark.parametrize("tier", list(Effort))
def test_every_effort_tier_fits_its_own_task_limit(tier):
    """The headline regression: for each seeded tier, the Celery soft limit
    must exceed that tier's own wall ceiling."""
    wall = _EFFORT_POLICIES[tier]["max_wall_seconds"]
    soft, _ = dispatch_time_limits(wall)
    if wall and wall > 0:
        assert soft > wall, f"{tier}: task limit {soft} <= wall ceiling {wall}"
    else:
        assert soft == MAX_DISPATCH_SECONDS, tier


def test_deep_tier_specifically_survives_past_the_old_flat_limit():
    """`deep` is the tier the old flat 3900 truncated."""
    soft, hard = dispatch_time_limits(_EFFORT_POLICIES[Effort.DEEP]["max_wall_seconds"])
    assert soft > 14400
    assert hard > 3900
