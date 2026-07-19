"""effective_cli_template — legacy seeded templates roll forward at dispatch,
human-tuned templates are preserved, empty resolves to the catalog default."""

from domains.llm_providers.schemas import (
    default_cli_template,
    effective_cli_template,
)

_LEGACY = (
    'claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} '
    '--mcp-config {{mcp_config_path_q}} '
    '--permission-mode bypassPermissions --output-format stream-json --verbose'
)


def test_legacy_claude_template_rolls_forward():
    out = effective_cli_template("claude_subscription", _LEGACY)
    assert out == default_cli_template("claude_subscription")
    assert "--append-system-prompt" in out
    assert "--system-prompt " not in out


def test_custom_template_is_preserved():
    custom = "claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} --my-flag"
    assert effective_cli_template("claude_subscription", custom) == custom


def test_empty_template_resolves_to_default():
    assert effective_cli_template("claude_subscription", "  ") == default_cli_template(
        "claude_subscription"
    )


def test_current_default_uses_append_system_prompt():
    assert "--append-system-prompt" in default_cli_template("claude_subscription")
