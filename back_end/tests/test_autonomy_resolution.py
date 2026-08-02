"""The autonomy tier: parse, precedence, caps.

Autonomy is the ATTENTION dial (how much to ask), deliberately separate from
Effort, the COMPUTE dial. Everything here is pure.
"""

from domains.runs.schemas import (
    AUTONOMY_QUESTION_CAPS,
    Autonomy,
    Effort,
    normalize_autonomy,
    normalize_effort,
)
from domains.runs.services.autonomy import question_cap, resolve_autonomy


def test_known_tiers_parse():
    assert normalize_autonomy("assume") is Autonomy.ASSUME
    assert normalize_autonomy("STRICT") is Autonomy.STRICT
    assert normalize_autonomy("  interrogate ") is Autonomy.INTERROGATE


def test_empty_reads_as_todays_behaviour():
    """Every row predating the dial stores "". It must mean "ask", or the
    migration silently makes existing threads more autonomous."""
    assert normalize_autonomy("") is Autonomy.INTERROGATE
    assert normalize_autonomy(None) is Autonomy.INTERROGATE


def test_unknown_falls_back_to_the_SAFEST_tier_not_the_middle():
    """The deliberate asymmetry with normalize_effort: an unreadable effort
    costs compute, an unreadable autonomy could cost a wrong product
    decision."""
    assert normalize_autonomy("aggressive") is Autonomy.INTERROGATE
    assert normalize_effort("nonsense") is Effort.NORMAL  # middle, for contrast


def test_precedence_request_beats_ticket_beats_repository():
    assert resolve_autonomy(
        requested="strict", ticket_override="assume", repository_default="interrogate"
    ) is Autonomy.STRICT
    assert resolve_autonomy(
        ticket_override="assume", repository_default="interrogate"
    ) is Autonomy.ASSUME
    assert resolve_autonomy(repository_default="assume") is Autonomy.ASSUME


def test_blank_layers_are_skipped_not_treated_as_a_choice():
    assert resolve_autonomy(
        requested="", ticket_override="  ", repository_default="strict"
    ) is Autonomy.STRICT


def test_nothing_configured_anywhere_is_interrogate():
    assert resolve_autonomy() is Autonomy.INTERROGATE


def test_a_typod_override_fails_SAFE_rather_than_inheriting():
    """`ticket_override="asume"` must not fall through to a more autonomous
    repository default — it resolves to interrogate."""
    assert resolve_autonomy(
        ticket_override="asume", repository_default="strict"
    ) is Autonomy.INTERROGATE


def test_caps_are_defined_for_every_tier():
    """One source of truth, consumed by both the prompt renderer and the
    server-side enforcement. A missing tier would KeyError at dispatch."""
    for tier in Autonomy:
        assert tier in AUTONOMY_QUESTION_CAPS


def test_cap_values():
    assert question_cap(Autonomy.INTERROGATE) is None  # uncapped
    assert question_cap("assume") == 3
    assert question_cap("strict") == 0


def test_cap_of_an_unknown_tier_is_uncapped_not_zero():
    """Failing safe means asking MORE, never silently muzzling the agent."""
    assert question_cap("nonsense") is None
