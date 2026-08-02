"""The question-policy block, and the promise that `interrogate` is unchanged.

The dial ships defaulting to `interrogate`, and the whole safety argument for
that default is "existing threads behave exactly as before". That is a claim
until something pins it — hence the golden string below, captured from the
literal that lived in `intents.py` before the parameterization.
"""

from types import SimpleNamespace

from domains.executors.prompt_kit import autonomy_block
from domains.threads.services.intents import build_thread_session_intent

# Verbatim from intents.py step 2, pre-dial. Do not "tidy" this string: its
# only job is to be the thing the code used to say.
GOLDEN_INTERROGATE = (
    "Interrogate the user: ask ALL currently independent clarifying "
    "questions in ONE turn — one `opensweep_platform_ask_user` call per "
    "question (thread_uid `th-1`, question, optional `options` "
    "list of 2-6 short choices) — then end your turn and wait. The "
    "platform holds the conversation until EVERY question is answered "
    "(or the user forces continue) and delivers all answers together; "
    "ask follow-ups that depend on earlier answers in a later round. "
    "Questions are also mirrored to the ticket's discussion, where the "
    "user can answer by replying. Surface trade-offs and your "
    "recommendation inside each question. Do NOT silently assume "
    "answers to open product questions."
)


def _ticket():
    return SimpleNamespace(
        uid="tk-1",
        title="Fix the thing",
        priority="medium",
        description="desc",
        acceptance_criteria=["a", "b"],
    )


def test_interrogate_is_byte_identical_to_the_pre_dial_wording():
    assert (
        autonomy_block(
            "interrogate",
            ask_tool="opensweep_platform_ask_user",
            subject_ref="thread_uid `th-1`",
            assumption_target="tk-1",
        )
        == GOLDEN_INTERROGATE
    )


def test_the_thread_intent_still_contains_that_exact_paragraph():
    """Guards the wiring, not just the renderer: a mis-passed subject_ref or a
    dropped parameter would still produce plausible prose."""
    intent = build_thread_session_intent(_ticket(), "th-1")
    assert GOLDEN_INTERROGATE in intent


def test_the_default_is_interrogate():
    """An un-configured thread — every existing one — must not change."""
    assert build_thread_session_intent(_ticket(), "th-1") == build_thread_session_intent(
        _ticket(), "th-1", "interrogate"
    )


def test_an_unknown_tier_renders_as_interrogate():
    assert build_thread_session_intent(
        _ticket(), "th-1", "aggressive"
    ) == build_thread_session_intent(_ticket(), "th-1", "interrogate")


def test_assume_states_the_cap_and_points_at_the_ledger():
    block = autonomy_block("assume", subject_ref="thread_uid `th-1`", assumption_target="tk-1")
    assert "at most 3" in block
    assert "opensweep_platform_record_assumption" in block
    assert "tk-1" in block, "the tool call must be pre-filled with the ticket"


def test_assume_tells_the_agent_to_read_the_code_first():
    """The behaviour that actually reduces questions — without it `assume` just
    truncates the interrogation instead of replacing it with judgment."""
    block = autonomy_block("assume", subject_ref="x", assumption_target="tk-1")
    assert "read the code" in block


def test_strict_refuses_rather_than_inventing_acceptance_criteria():
    block = autonomy_block("strict", subject_ref="x", assumption_target="tk-1")
    assert "Ask NOTHING" in block
    assert "complete_run" in block
    assert "refusing to guess" in block


def test_strict_never_offers_the_ask_tool():
    """A tier whose whole point is "don't ask" must not name the ask tool at
    all — mentioning it is an invitation."""
    block = autonomy_block(
        "strict", ask_tool="opensweep_platform_ask_user", subject_ref="x"
    )
    assert "opensweep_platform_ask_user" not in block


def test_the_block_is_reusable_for_a_non_thread_subject():
    """Batch triage passes a different tool and a run-scoped subject."""
    block = autonomy_block(
        "assume",
        ask_tool="opensweep_platform_ask_policy_question",
        subject_ref="run_uid `r-9`",
        assumption_target="tk-1",
    )
    assert "opensweep_platform_ask_policy_question" in block
    assert "run_uid `r-9`" in block
