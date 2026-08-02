"""Reasoning level → provider-specific knobs.

One pure mapping so every adapter agrees on what "low/medium/high" means per
provider kind (domains/llm_providers/schemas.LLMProviderKind values):

- claude_subscription: the Claude Code CLI reads MAX_THINKING_TOKENS from the
  environment. "low" disables thinking ("0"); "medium" omits the key (the CLI
  default IS medium); "high" raises the budget to 31999.
- opencode: no knob — reasoning behaviour is a property of the configured
  model/endpoint (see llm_executor._prepare_opencode_config).

Unknown kinds (and level == "") get no knobs at all: {}.
"""

from __future__ import annotations

_LEVELS = {"low", "medium", "high"}

# Claude Code CLI env budgets ("medium" omits the key — the CLI default).
_CLAUDE_CLI_THINKING_TOKENS = {"low": "0", "high": "31999"}


def reasoning_args(level: str, provider_kind: str) -> dict:
    """Provider knobs for a reasoning level, keyed by transport:

    {"env": {...}} — subprocess environment additions (claude CLI)

    Empty dict when the level is unset/unknown or the kind takes no knob.
    """
    lvl = (level or "").strip().lower()
    kind = (provider_kind or "").strip()
    if lvl not in _LEVELS:
        return {}

    if kind == "claude_subscription":
        tokens = _CLAUDE_CLI_THINKING_TOKENS.get(lvl)
        return {"env": {"MAX_THINKING_TOKENS": tokens}} if tokens is not None else {}

    return {}
