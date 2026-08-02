"""reasoning_args — pure reasoning-level → provider-knob mapping — and
resolve_reasoning (agent override vs. effort-tier default)."""

from domains.executors.reasoning import reasoning_args
from domains.runs.schemas import (
    REASONING_TIER_DEFAULTS,
    Effort,
    resolve_reasoning,
)


# ── reasoning_args ───────────────────────────────────────────────────────────


def test_claude_subscription_env_budgets():
    assert reasoning_args("low", "claude_subscription") == {
        "env": {"MAX_THINKING_TOKENS": "0"}
    }
    # "medium" is the CLI default — the key is omitted entirely.
    assert reasoning_args("medium", "claude_subscription") == {}
    assert reasoning_args("high", "claude_subscription") == {
        "env": {"MAX_THINKING_TOKENS": "31999"}
    }


def test_unknown_kind_returns_empty():
    assert reasoning_args("high", "opencode") == {}
    assert reasoning_args("high", "aider") == {}
    assert reasoning_args("high", "custom") == {}
    assert reasoning_args("high", "not-a-kind") == {}
    assert reasoning_args("high", "") == {}


def test_unset_or_garbage_level_returns_empty():
    for kind in ("claude_subscription", "codex_subscription", "claude_api", "openai_api", "ollama"):
        assert reasoning_args("", kind) == {}
        assert reasoning_args("extreme", kind) == {}


# ── resolve_reasoning ────────────────────────────────────────────────────────


def test_tier_defaults_cover_every_tier():
    assert REASONING_TIER_DEFAULTS == {
        Effort.SHORT: "low",
        Effort.NORMAL: "medium",
        Effort.DEEP: "high",
        Effort.UNLIMITED: "high",
    }


def test_resolve_reasoning_inherits_tier_default():
    assert resolve_reasoning("", Effort.SHORT) == "low"
    assert resolve_reasoning("", Effort.NORMAL) == "medium"
    assert resolve_reasoning("", Effort.DEEP) == "high"
    assert resolve_reasoning("", Effort.UNLIMITED) == "high"


def test_resolve_reasoning_agent_override_wins():
    assert resolve_reasoning("low", Effort.DEEP) == "low"
    assert resolve_reasoning("high", Effort.SHORT) == "high"
    assert resolve_reasoning("Medium", Effort.SHORT) == "medium"  # tolerant case


def test_resolve_reasoning_garbage_falls_back_to_tier():
    assert resolve_reasoning("extreme", Effort.NORMAL) == "medium"
    assert resolve_reasoning(None, Effort.DEEP) == "high"  # type: ignore[arg-type]
