"""Golden-file tests for the prompt kit.

The four system prompts and the four stance tiers are pinned byte-for-byte:
prompt text is an interface (agents are tuned against it), so drift must be a
deliberate, reviewed change. Regenerate with:

    UPDATE_GOLDENS=1 .venv/bin/python -m pytest tests/test_prompt_goldens.py
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from domains.executors.prompt_kit import stance_block, system_prompt

GOLDENS_DIR = Path(__file__).parent / "goldens"

KINDS = ("claude_code_read", "claude_code_write", "internal_llm", "cli_tracking")
TIERS = ("short", "normal", "deep", "unlimited")

# Fixed fake policy so the goldens are deterministic.
_POLICY = SimpleNamespace(
    max_wall_seconds=3600,
    max_tool_turns=200,
    max_files_touched=100,
    max_continuation_passes=3,
    warn_at_pct=80,
)
_WALL_CEILING = 3600


def _assert_matches_golden(name: str, rendered: str) -> None:
    path = GOLDENS_DIR / name
    if os.environ.get("UPDATE_GOLDENS"):
        GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        # A regeneration run must never report green: it writes tracked source
        # files and performs no verification. Failing here stops a
        # rubber-stamped golden from being committed with a passing suite.
        pytest.fail(
            f"golden {name} regenerated — rerun without UPDATE_GOLDENS and "
            "review the diff before committing"
        )
    assert path.exists(), f"golden {name} missing — regenerate with UPDATE_GOLDENS=1"
    assert rendered == path.read_text(), (
        f"{name} drifted from its golden — if the change is intentional, "
        "regenerate with UPDATE_GOLDENS=1 and review the diff"
    )


@pytest.mark.parametrize("kind", KINDS)
def test_system_prompt_matches_golden(kind):
    _assert_matches_golden(f"prompt_{kind}.txt", system_prompt(kind))


@pytest.mark.parametrize("tier", TIERS)
def test_stance_block_matches_golden(tier):
    _assert_matches_golden(
        f"stance_{tier}.txt", stance_block(_POLICY, _WALL_CEILING, tier)
    )


def test_no_dollar_language_anywhere():
    texts = [system_prompt(k) for k in KINDS]
    texts += [stance_block(_POLICY, _WALL_CEILING, t) for t in TIERS]
    for text in texts:
        assert "$" not in text
        assert "dollar" not in text.lower()


def test_envelope_contract_present_for_envelope_kinds():
    for kind in ("internal_llm", "cli_tracking"):
        text = system_prompt(kind)
        assert "```json" in text
        assert '"tool_calls"' in text
        assert "complete_run" in text


def test_tool_list_present_in_every_kind():
    for kind in KINDS:
        text = system_prompt(kind)
        assert "create_finding" in text
        assert "complete_run" in text
