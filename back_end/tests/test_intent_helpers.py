"""Tests for the shared intent helpers + look-before-write footer."""

from domains.runs.services._intent_helpers import (
    OPENSWEEP_FRAMING_HEADER,
    LOOK_BEFORE_WRITE_FOOTER,
    build_intent,
)


def test_footer_names_the_required_read_tools():
    """Names must be the ones the agent can actually call.

    This assertion used to pin `opensweep_list_findings` — a spelling that
    resolved to nothing: the served MCP operation is
    `opensweep_platform_read_list_findings`, and the tool was not in the
    dispatcher registry either, so no tool list advertised it. The test was
    enforcing the bug. test_prompt_tool_names_resolve now checks every tool
    name in every shipped prompt against the real surfaces.
    """
    assert "list_docs" in LOOK_BEFORE_WRITE_FOOTER
    assert "read_doc" in LOOK_BEFORE_WRITE_FOOTER
    assert "search_memory" in LOOK_BEFORE_WRITE_FOOTER
    assert "list_findings" in LOOK_BEFORE_WRITE_FOOTER
    assert "search_findings" in LOOK_BEFORE_WRITE_FOOTER
    assert "get_finding" in LOOK_BEFORE_WRITE_FOOTER


def test_footer_lays_out_the_decision_steps():
    body = LOOK_BEFORE_WRITE_FOOTER
    assert "SEARCH" in body
    assert "GET" in body
    assert "DECIDE" in body
    assert "evidence.rationale" in body


def test_build_intent_assembles_sections():
    out = build_intent(
        default_intent="Default body",
        scope_summary="repo=abc",
        existing_state_listing="- uid=1 name='X'",
    )
    assert "Default body" in out
    assert "repo=abc" in out
    assert "- uid=1" in out
    assert "look-before-write" in out.lower()


def test_build_intent_prefers_prompt_body_over_default():
    out = build_intent(
        prompt_body="Prompt body",
        default_intent="Default body",
    )
    assert "Prompt body" in out
    assert "Default body" not in out


def test_build_intent_prefers_custom_over_prompt():
    out = build_intent(
        prompt_body="Prompt body",
        custom_intent="Custom override",
        default_intent="Default body",
    )
    assert "Custom override" in out
    assert "Prompt body" not in out


def test_build_intent_can_skip_footer():
    out = build_intent(default_intent="Body", include_footer=False)
    assert "Body" in out
    assert "# Look-before-write contract" not in out


def test_build_intent_always_includes_framing_header():
    """Org-agent-overlays spec: the instructions layer comes from editable
    rows, so the identity / output-shape re-anchoring is unconditional."""
    out = build_intent(default_intent="OpenSweep-native default body")
    assert "OpenSweep-native default body" in out
    assert "You are OpenSweep" in out
    assert out.index("You are OpenSweep") < out.index("OpenSweep-native default body")


def test_build_intent_can_skip_header_for_thin_wrappers():
    out = build_intent(default_intent="Body", include_header=False, include_footer=False)
    assert "You are OpenSweep" not in out


def test_build_intent_prepends_opensweep_framing_when_prompt_body_used():
    out = build_intent(
        prompt_body="You are a senior code reviewer. ## Output Format ...",
        default_intent="OpenSweep-native default body",
    )
    assert "You are OpenSweep" in out
    assert "create_finding" in out
    assert "You are a senior code reviewer" in out
    assert out.index("You are OpenSweep") < out.index("You are a senior code reviewer")


def test_build_intent_prepends_opensweep_framing_when_custom_intent_used():
    out = build_intent(
        custom_intent="Find SQL injection in the api layer.",
        default_intent="OpenSweep-native default body",
    )
    assert "You are OpenSweep" in out
    assert "Find SQL injection in the api layer." in out


def test_opensweep_framing_header_overrides_output_shape_and_persona():
    assert "tool calls" in OPENSWEEP_FRAMING_HEADER.lower()
    assert "create_finding" in OPENSWEEP_FRAMING_HEADER
    assert "propose_doc_edit" in OPENSWEEP_FRAMING_HEADER
    assert "write_memory" in OPENSWEEP_FRAMING_HEADER
    # The header still has to re-anchor identity — that is its whole job.
    assert "You are OpenSweep" in OPENSWEEP_FRAMING_HEADER


def test_opensweep_framing_header_avoids_jailbreak_classifier_phrasing():
    """The header is prepended to EVERY intent, so it must survive provider-side
    prompt-injection classifiers.

    Azure AI Foundry's Prompt Shield reads "ignore any <instructions>" as an
    attack and fails the whole request with HTTP 400
    (`ResponsibleAIPolicyViolation`, jailbreak detected) — it cannot tell our
    anti-injection defense from an actual injection. Every OpenSweep run against
    such a deployment failed before the response was even generated.

    Express the same two constraints positively ("your identity is set here",
    "take its substance, not its formatting") rather than as an imperative to
    disregard the body.
    """
    lowered = OPENSWEEP_FRAMING_HEADER.lower()
    for trigger in ("ignore any", "ignore all", "disregard any", "disregard all"):
        assert trigger not in lowered, (
            f"{trigger!r} in the framing header trips provider jailbreak "
            "classifiers — see the docstring above"
        )
