"""Which autonomy tier a run gets. Pure.

Mirrors how `Effort` resolves (agent default → scheduled-agent override →
hardcoded tier at the delivery services), but scoped down: autonomy only means
anything on runs that can reach a human, so the chain is per-ticket and
per-repository rather than per-agent.

Kept pure and separate from dispatch so the precedence is testable without a
DB — the same split `refine_dispatch` uses for its intent builder.
"""

from __future__ import annotations

from domains.runs.schemas import AUTONOMY_QUESTION_CAPS, Autonomy, normalize_autonomy


def resolve_autonomy(
    *,
    requested: str = "",
    ticket_override: str = "",
    repository_default: str = "",
) -> Autonomy:
    """First non-empty wins: request → ticket → repository → INTERROGATE.

    The ticket layer exists because "this one is a known-shape chore, don't
    interrogate me" is exactly the per-item judgment a repository-wide default
    cannot make. `""` at any layer means inherit, matching
    `ScheduledAgent.effort`'s convention.

    An unrecognized value does NOT fall through to the next layer — it resolves
    to INTERROGATE via `normalize_autonomy`. A typo'd override must fail safe
    (ask) rather than silently inherit something more autonomous.
    """
    for candidate in (requested, ticket_override, repository_default):
        if (candidate or "").strip():
            return normalize_autonomy(candidate)
    return Autonomy.INTERROGATE


def question_cap(autonomy: str | Autonomy) -> int | None:
    """Questions allowed for this tier. None = uncapped."""
    tier = autonomy if isinstance(autonomy, Autonomy) else normalize_autonomy(autonomy)
    return AUTONOMY_QUESTION_CAPS[tier]
