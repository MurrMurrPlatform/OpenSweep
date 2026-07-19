"""Per-run ceiling enforcement.

PLATFORM.md §Run policies: operational ceilings (max_wall_seconds /
max_tool_turns / max_files_touched / max_test_seconds)
apply to every executor. Cost ceilings (max_tokens / max_dollars) apply
only where the executor exposes them -- primarily internal_llm with an
API provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from domains.run_policies.models import RunPolicy


class CeilingExceeded(RuntimeError):
    """Raised only when a caller passes raise_on_exceed=True.

    No runtime caller does this anymore — ceilings are now warnings-only
    (adapters never hard-stop a run on usage). The class and flag are kept for
    API stability and are still exercised by tests.
    """

    def __init__(self, field: str, value: float, ceiling: float) -> None:
        self.field = field
        self.value = value
        self.ceiling = ceiling
        super().__init__(f"ceiling {field}: {value} >= {ceiling}")


class CeilingWarning(Warning):
    """Soft-warn when usage crosses warn_at_pct of any ceiling.

    Adapters log + push to Run.usage warnings but continue.
    """


@dataclass
class UsageSnapshot:
    wall_seconds: float = 0.0
    tool_turns: int = 0
    files_touched: int = 0
    test_seconds: float = 0.0
    tokens: int = 0
    dollars: float = 0.0


_FIELDS = [
    ("wall_seconds", "max_wall_seconds"),
    ("tool_turns", "max_tool_turns"),
    ("files_touched", "max_files_touched"),
    ("test_seconds", "max_test_seconds"),
    ("tokens", "max_tokens"),
    ("dollars", "max_dollars"),
]


def check(
    *,
    policy: RunPolicy,
    usage: UsageSnapshot,
    raise_on_exceed: bool = True,
) -> list[str]:
    """Return a list of soft warnings; raise on first hard exceedance.

    Cost fields (tokens/dollars) are silently ignored when no pricing was
    surfaced (usage.tokens == 0 and usage.dollars == 0.0) — that's the
    "subscription executor" case where the platform cannot meter.
    """
    warn_pct = (policy.warn_at_pct or 80) / 100.0
    warnings: list[str] = []
    for usage_field, policy_field in _FIELDS:
        ceiling = getattr(policy, policy_field, None)
        if ceiling is None or ceiling <= 0:
            continue
        value = float(getattr(usage, usage_field, 0) or 0)
        if usage_field in ("tokens", "dollars") and value == 0.0:
            continue
        if value >= ceiling and raise_on_exceed:
            raise CeilingExceeded(field=policy_field, value=value, ceiling=float(ceiling))
        if value >= ceiling * warn_pct:
            warnings.append(
                f"{policy_field}: {value} >= {warn_pct * 100:.0f}% of {ceiling}"
            )
    return warnings


def remaining(*, policy: RunPolicy, usage: UsageSnapshot) -> dict[str, Optional[float]]:
    """Slack remaining per ceiling — useful for surfaces that want a 'budget bar'."""
    out: dict[str, Optional[float]] = {}
    for usage_field, policy_field in _FIELDS:
        ceiling = getattr(policy, policy_field, None)
        if ceiling is None or ceiling <= 0:
            out[policy_field] = None
            continue
        value = float(getattr(usage, usage_field, 0) or 0)
        out[policy_field] = max(0.0, float(ceiling) - value)
    return out
