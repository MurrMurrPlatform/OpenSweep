"""Effort → policy mapping: 4 tiers, legacy 'quick' normalizes to short,
wall sentinel 0 = explicitly unlimited."""

from domains.executors.base import DispatchRequest
from domains.executors._shared import resolve_wall_ceiling
from domains.runs.schemas import Effort, normalize_effort
from domains.run_policies.services.effort import _EFFORT_POLICIES
from domains.run_policies.services.system_default import _DEFAULTS


def test_normalize_effort_accepts_legacy_quick():
    assert normalize_effort("quick") is Effort.SHORT
    assert normalize_effort("short") is Effort.SHORT
    assert normalize_effort("deep") is Effort.DEEP
    assert normalize_effort("unlimited") is Effort.UNLIMITED
    assert normalize_effort("") is Effort.NORMAL
    assert normalize_effort("garbage") is Effort.NORMAL


def test_legacy_seed_effort_values_normalize():
    """Agents store default_effort as free strings (seeds use 'quick'/'light');
    dispatch must normalize instead of raising via a raw Effort() call."""
    assert normalize_effort("light") is Effort.SHORT
    assert normalize_effort("small") is Effort.SHORT
    assert normalize_effort("large") is Effort.DEEP
    assert normalize_effort("quick") is Effort.SHORT


def test_four_effort_tiers_have_policies():
    assert set(_EFFORT_POLICIES) == {
        Effort.SHORT,
        Effort.NORMAL,
        Effort.DEEP,
        Effort.UNLIMITED,
    }
    assert _EFFORT_POLICIES[Effort.UNLIMITED]["max_wall_seconds"] == 0
    assert _EFFORT_POLICIES[Effort.UNLIMITED]["max_tool_turns"] is None


def test_tier_operational_ceilings():
    expected = {
        Effort.SHORT: (900, 50, 25, 1),
        Effort.NORMAL: (3600, 200, 100, 3),
        Effort.DEEP: (14400, 3000, 10000, 8),
        Effort.UNLIMITED: (0, None, None, None),
    }
    for tier, (wall, turns, files, passes) in expected.items():
        config = _EFFORT_POLICIES[tier]
        assert config["max_wall_seconds"] == wall
        assert config["max_tool_turns"] == turns
        assert config["max_files_touched"] == files
        assert config["max_continuation_passes"] == passes


def test_no_tier_carries_money_ceilings():
    for config in _EFFORT_POLICIES.values():
        assert "max_dollars" not in config
        assert "max_tokens" not in config


def test_system_default_is_unlimited():
    assert _DEFAULTS["max_wall_seconds"] == 0
    assert _DEFAULTS["max_tool_turns"] is None
    assert _DEFAULTS["max_continuation_passes"] is None
    assert "max_dollars" not in _DEFAULTS


class _P:
    def __init__(self, wall):
        self.max_wall_seconds = wall


class _Provider:
    def __init__(self, kind: str, base_url: str = "") -> None:
        self.kind = kind
        self.base_url = base_url


# claude_subscription is always cloud (Anthropic-hosted), so the wall ceiling
# ladder falls through to policy/system-default regardless of base_url.
_CLAUDE = _Provider("claude_subscription")


def _req(policy):
    return DispatchRequest(
        run_uid="r", scheduled_agent_uid="", repository_uid="repo",
        repository_local_path=None, intent="x", policy=policy,
    )


def test_wall_sentinel_zero_disables_guard():
    assert resolve_wall_ceiling(_req(_P(0)), _CLAUDE) is None


def test_wall_positive_is_used():
    assert resolve_wall_ceiling(_req(_P(7200)), _CLAUDE) == 7200


def test_wall_unset_falls_back_to_system_default():
    from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS
    assert resolve_wall_ceiling(_req(_P(None)), _CLAUDE) == DEFAULT_MAX_WALL_SECONDS


# Legacy "quick" must normalize at every API boundary that types the field as
# Effort — otherwise old clients / stored payloads 422 (the enum
# no longer has a "quick" member).


def test_trigger_review_request_normalizes_quick_depth():
    from api.v1.delivery import TriggerReviewRequest

    assert TriggerReviewRequest(depth="quick").depth is Effort.SHORT


def test_trigger_review_request_defaults_depth_to_normal():
    from api.v1.delivery import TriggerReviewRequest

    assert TriggerReviewRequest().depth is Effort.NORMAL


def test_audit_request_normalizes_quick_effort():
    from api.v1.sweep import AuditRequest

    assert AuditRequest(effort="quick").effort is Effort.SHORT


def test_deep_scan_request_normalizes_quick_effort():
    from api.v1.sweep import DeepScanRequest

    assert DeepScanRequest(effort="quick").effort is Effort.SHORT


def test_deep_scan_request_defaults_effort_to_deep():
    from api.v1.sweep import DeepScanRequest

    assert DeepScanRequest().effort is Effort.DEEP
