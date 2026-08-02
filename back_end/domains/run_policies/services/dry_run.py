"""Dry-run estimator.

PLATFORM.md §Run policies, rule 2: `dry_run` is a first-class executor
mode. It estimates context size + an executor-specific complexity guess
and returns *without* invoking.

This estimator is deliberately rough — it's a budget signal for the user
before kicking off a paid Run, not a fine-grained forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domains.runs.schemas import Executor


# Rough chars-per-token + per-1k-token dollar estimates. Only the
# Anthropic/OpenAI-shaped executors have a meaningful dollar number; for
# subscription executors we return None and surface "usage proxies" only.
_CHARS_PER_TOKEN = 4.0

# claude_code / opencode → subscription / local → no dollar figure. The
# mechanism stays for any future metered executor.
_DOLLARS_PER_1K_TOK: dict[Executor, float] = {}


@dataclass
class DryRunEstimate:
    executor: Executor
    estimated_input_tokens: int = 0
    estimated_input_chars: int = 0
    estimated_dollars: float | None = None
    estimated_wall_seconds: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor.value,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_input_chars": self.estimated_input_chars,
            "estimated_dollars": self.estimated_dollars,
            "estimated_wall_seconds": self.estimated_wall_seconds,
            "notes": list(self.notes),
        }


def estimate(
    *,
    executor: Executor,
    intent: str,
    context_chars: int = 0,
    scope_paths: int = 0,
) -> DryRunEstimate:
    chars = len(intent or "") + max(0, int(context_chars))
    tokens = int(chars / _CHARS_PER_TOKEN) if chars else 0

    # Subscription / local executors expose no pricing; usage proxies only.
    dollars: float | None = None
    if executor in _DOLLARS_PER_1K_TOK:
        dollars = round((tokens / 1000.0) * _DOLLARS_PER_1K_TOK[executor], 4)

    # Executor-specific complexity guess.
    base_secs = {
        Executor.CLAUDE_CODE: 45,
        Executor.OPENCODE: 30,
        Executor.MANUAL: 0,
    }.get(executor, 30)
    # Each additional scope path → assume ~2s of investigation overhead.
    wall = base_secs + 2 * max(0, scope_paths)

    notes: list[str] = []
    if dollars is None:
        notes.append(
            "subscription/local executor: tracked via usage proxies (wall-time, "
            "tool turns, files touched) — no dollar metering"
        )

    return DryRunEstimate(
        executor=executor,
        estimated_input_tokens=tokens,
        estimated_input_chars=chars,
        estimated_dollars=dollars,
        estimated_wall_seconds=wall,
        notes=notes,
    )
