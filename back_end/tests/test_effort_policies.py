"""Effort → policy mapping: 4 tiers, legacy 'quick' normalizes to short,
wall sentinel 0 = explicitly unlimited."""

from domains.executors.base import DispatchRequest
from domains.executors._shared import resolve_wall_ceiling
from domains.investigations.schemas import (
    InvestigationEffort,
    UpdateInvestigationRequest,
    normalize_effort,
)
from domains.run_policies.services.effort import _EFFORT_POLICIES
from domains.run_policies.services.system_default import _DEFAULTS


def test_normalize_effort_accepts_legacy_quick():
    assert normalize_effort("quick") is InvestigationEffort.SHORT
    assert normalize_effort("short") is InvestigationEffort.SHORT
    assert normalize_effort("deep") is InvestigationEffort.DEEP
    assert normalize_effort("unlimited") is InvestigationEffort.UNLIMITED
    assert normalize_effort("") is InvestigationEffort.NORMAL
    assert normalize_effort("garbage") is InvestigationEffort.NORMAL


def test_update_request_preserves_none_effort():
    """PATCH omitting effort must leave it as None — not coerce to NORMAL."""
    req = UpdateInvestigationRequest()
    assert req.effort is None


def test_update_request_normalizes_quick_to_short():
    """Legacy 'quick' in a PATCH body must normalize to SHORT, not fall through."""
    req = UpdateInvestigationRequest(effort="quick")
    assert req.effort is InvestigationEffort.SHORT


def test_four_effort_tiers_have_policies():
    assert set(_EFFORT_POLICIES) == {
        InvestigationEffort.SHORT,
        InvestigationEffort.NORMAL,
        InvestigationEffort.DEEP,
        InvestigationEffort.UNLIMITED,
    }
    assert _EFFORT_POLICIES[InvestigationEffort.UNLIMITED]["max_wall_seconds"] == 0
    assert _EFFORT_POLICIES[InvestigationEffort.UNLIMITED]["max_tool_turns"] is None


def test_system_default_is_unlimited():
    assert _DEFAULTS["max_wall_seconds"] == 0
    assert _DEFAULTS["max_tool_turns"] is None
    assert _DEFAULTS["max_dollars"] is None


class _P:
    def __init__(self, wall):
        self.max_wall_seconds = wall


def _req(policy):
    return DispatchRequest(
        run_uid="r", investigation_uid="i", repository_uid="repo",
        repository_local_path=None, intent="x", policy=policy,
    )


def test_wall_sentinel_zero_disables_guard():
    assert resolve_wall_ceiling(_req(_P(0)), "claude_subscription") is None


def test_wall_positive_is_used():
    assert resolve_wall_ceiling(_req(_P(7200)), "claude_subscription") == 7200


def test_wall_unset_falls_back_to_system_default():
    from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS
    assert resolve_wall_ceiling(_req(_P(None)), "claude_subscription") == DEFAULT_MAX_WALL_SECONDS


# Legacy "quick" must normalize at every API boundary that types the field as
# InvestigationEffort — otherwise old clients / stored payloads 422 (the enum
# no longer has a "quick" member).


def test_trigger_review_request_normalizes_quick_depth():
    from api.v1.delivery import TriggerReviewRequest

    assert TriggerReviewRequest(depth="quick").depth is InvestigationEffort.SHORT


def test_trigger_review_request_defaults_depth_to_normal():
    from api.v1.delivery import TriggerReviewRequest

    assert TriggerReviewRequest().depth is InvestigationEffort.NORMAL


def test_audit_request_normalizes_quick_effort():
    from api.v1.sweep import AuditRequest

    assert AuditRequest(effort="quick").effort is InvestigationEffort.SHORT


def test_deep_scan_request_normalizes_quick_effort():
    from api.v1.sweep import DeepScanRequest

    assert DeepScanRequest(effort="quick").effort is InvestigationEffort.SHORT


def test_deep_scan_request_defaults_effort_to_deep():
    from api.v1.sweep import DeepScanRequest

    assert DeepScanRequest().effort is InvestigationEffort.DEEP
