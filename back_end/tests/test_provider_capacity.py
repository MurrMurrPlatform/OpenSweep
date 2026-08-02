"""Per-provider concurrency ceiling (LLMProvider.max_concurrent_runs)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from domains.llm_providers.schemas import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    default_max_concurrent_runs,
)
from domains.llm_providers.services import capacity


def _provider(uid="p-1", ceiling=5, kind="claude_subscription"):
    return SimpleNamespace(uid=uid, max_concurrent_runs=ceiling, kind=kind)


def test_other_kinds_get_the_platform_default():
    for kind in ("claude_subscription", "opencode"):
        assert default_max_concurrent_runs(kind) == DEFAULT_MAX_CONCURRENT_RUNS


def test_unknown_kind_falls_back_to_the_default():
    assert default_max_concurrent_runs("not-a-kind") == DEFAULT_MAX_CONCURRENT_RUNS


def test_configured_ceiling_defaults_rows_written_before_m0016():
    assert capacity.configured_ceiling(_provider(ceiling=None)) == (
        DEFAULT_MAX_CONCURRENT_RUNS
    )


def test_unset_ceiling_falls_back_to_the_KIND_default_not_the_platform_one():
    """An unset row resolves through the KIND default (which currently
    equals the platform default for both kinds)."""
    assert capacity.configured_ceiling(
        _provider(ceiling=0, kind="opencode")
    ) == DEFAULT_MAX_CONCURRENT_RUNS
    assert capacity.configured_ceiling(
        _provider(ceiling=None, kind="opencode")
    ) == DEFAULT_MAX_CONCURRENT_RUNS


def test_configured_ceiling_never_returns_zero_or_negative():
    assert capacity.configured_ceiling(_provider(ceiling=0)) >= 1
    assert capacity.configured_ceiling(_provider(ceiling=-3)) >= 1


def test_explicit_ceiling_wins_over_the_kind_default():
    """An operator who raised a provider to 3 gets 3 — the ceiling is
    advice about capacity, not an override of the lease."""
    assert capacity.configured_ceiling(
        _provider(ceiling=3, kind="opencode")
    ) == 3


@pytest.mark.asyncio
async def test_headroom_is_ceiling_minus_active_runs():
    with patch.object(capacity, "active_run_count", return_value=2):
        assert await capacity.provider_headroom(_provider(ceiling=5)) == 3


@pytest.mark.asyncio
async def test_headroom_floors_at_zero_when_oversubscribed():
    """Runs started before the ceiling was lowered must not produce negative
    headroom — plan_tick would read that as unbounded via max()."""
    with patch.object(capacity, "active_run_count", return_value=9):
        assert await capacity.provider_headroom(_provider(ceiling=5)) == 0


@pytest.mark.asyncio
async def test_no_provider_does_not_clamp():
    assert await capacity.provider_headroom(None) == DEFAULT_MAX_CONCURRENT_RUNS


@pytest.mark.asyncio
async def test_active_count_uses_the_shared_in_flight_definition():
    """paused_quota occupies a slot: the resume beat re-dispatches that very
    run, so it has not released the provider."""
    runs = [
        SimpleNamespace(status="running"),
        SimpleNamespace(status="queued"),
        SimpleNamespace(status="paused_quota"),
        SimpleNamespace(status="ended"),
        SimpleNamespace(status="awaiting_input"),
        SimpleNamespace(status="failed"),
    ]

    class _Nodes:
        @staticmethod
        async def filter(**_):
            return runs

    with patch.object(capacity.Run, "nodes", _Nodes()):
        assert await capacity.active_run_count("p-1") == 3


@pytest.mark.asyncio
async def test_blank_provider_uid_never_queries():
    assert await capacity.active_run_count("") == 0


# ── Admission gate ──────────────────────────────────────────────────────────
# The boundary the write path enforces. Pure, so it is stated once here and
# nowhere else.


def test_a_slot_below_the_ceiling_admits():
    assert capacity.admission_refusal(active=4, ceiling=5, provider_label="Claude") is None


def test_at_the_ceiling_refuses():
    """`active == ceiling` is FULL, not "one more allowed". Off-by-one here is
    the difference between honouring max_concurrent_runs and exceeding it by
    exactly one on every provider."""
    refusal = capacity.admission_refusal(active=5, ceiling=5, provider_label="Claude")
    assert refusal is not None
    assert "5/5" in refusal


def test_over_the_ceiling_refuses():
    assert capacity.admission_refusal(active=9, ceiling=5, provider_label="Claude") is not None


def test_an_empty_ceiling_refuses_everything():
    """A provider configured to 0 takes no runs at all. `configured_ceiling`
    never returns 0 today, but the gate must not read 0 as "unlimited"."""
    assert capacity.admission_refusal(active=0, ceiling=0, provider_label="x") is not None


def test_refusal_names_the_provider_and_survives_a_blank_label():
    assert "Claude Sub" in capacity.admission_refusal(
        active=5, ceiling=5, provider_label="Claude Sub"
    )
    assert "the LLM provider" in capacity.admission_refusal(
        active=5, ceiling=5, provider_label="  "
    )


def test_admission_agrees_with_headroom_at_every_boundary():
    """The two must never disagree: headroom > 0 iff admission is allowed.
    They are separate functions with separate callers (campaign clamp vs
    dispatch gate), and a drift between them is precisely the double-count the
    composition argument rules out."""
    ceiling = 5
    for active in range(0, ceiling + 3):
        headroom = max(ceiling - active, 0)
        admits = capacity.admission_refusal(
            active=active, ceiling=ceiling, provider_label="p"
        ) is None
        assert admits == (headroom > 0), f"disagreement at active={active}"
