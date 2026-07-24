"""with_model_flag — the provider's model (incl. per-stage workflow override)
must reach claude/codex CLIs whose templates have no {{model}} placeholder.

The workflow override is applied in-memory to `provider.model` before argv
construction, so injecting `provider.model` covers both the provider's own
model and the per-stage override.
"""

import shlex

import pytest

from domains.llm_providers.schemas import default_cli_template
from domains.llm_providers.services.llm_executor import (
    _render_template,
    with_codex_sandbox_bypass,
    with_model_flag,
)

_BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def test_claude_gets_model_appended_when_template_lacks_placeholder():
    argv = ["claude", "-p", "hi", "--output-format", "stream-json"]
    out = with_model_flag(
        argv, kind="claude_subscription", model="claude-sonnet-4-6", template="claude -p {{instruction_q}}"
    )
    assert out[-2:] == ["--model", "claude-sonnet-4-6"]


def test_template_with_model_placeholder_is_left_alone():
    argv = ["claude", "-p", "hi", "--model", "claude-opus-4-8"]
    out = with_model_flag(
        argv,
        kind="claude_subscription",
        model="claude-opus-4-8",
        template="claude -p {{instruction_q}} --model {{model_q}}",
    )
    assert out == argv


def test_explicit_model_flag_in_argv_is_not_duplicated():
    argv = ["claude", "-p", "hi", "--model", "claude-opus-4-8"]
    out = with_model_flag(
        argv, kind="claude_subscription", model="claude-sonnet-4-6", template="claude -p {{instruction_q}} --model claude-opus-4-8"
    )
    assert out == argv


def test_empty_model_injects_nothing():
    argv = ["claude", "-p", "hi"]
    assert with_model_flag(argv, kind="claude_subscription", model="  ", template="claude -p x") == argv


def test_codex_model_lands_after_exec_subcommand():
    argv = ["codex", "exec", "--skip-git-repo-check", "--json", "prompt"]
    out = with_model_flag(
        argv, kind="codex_subscription", model="gpt-5-codex", template="codex exec --skip-git-repo-check --json {{instruction_q}}"
    )
    assert out[:4] == ["codex", "exec", "--model", "gpt-5-codex"]
    assert out[4:] == ["--skip-git-repo-check", "--json", "prompt"]


def test_codex_short_model_flag_is_respected():
    argv = ["codex", "exec", "-m", "gpt-5-codex", "prompt"]
    out = with_model_flag(
        argv, kind="codex_subscription", model="other", template="codex exec -m gpt-5-codex {{instruction_q}}"
    )
    assert out == argv


def test_other_kinds_pass_through():
    argv = ["opencode", "run", "hi"]
    assert with_model_flag(argv, kind="opencode", model="x", template="opencode run {{instruction_q}}") == argv


# ── with_codex_sandbox_bypass — codex must not run its own OS sandbox ──────────
# OpenSweep already isolates the run; codex's landlock/seccomp sandbox can't
# create a namespace in the worker container (every shell command fails) and its
# approval policy auto-cancels MCP tool calls. The bypass flag fixes both.


def test_bypass_inserted_right_after_exec():
    argv = ["codex", "exec", "--skip-git-repo-check", "--json", "prompt"]
    out = with_codex_sandbox_bypass(argv)
    assert out[:3] == ["codex", "exec", _BYPASS]
    assert out[3:] == ["--skip-git-repo-check", "--json", "prompt"]


def test_bypass_is_idempotent():
    argv = ["codex", "exec", _BYPASS, "--json", "prompt"]
    assert with_codex_sandbox_bypass(argv) == argv


def test_bypass_defers_to_an_explicit_sandbox_flag():
    argv = ["codex", "exec", "--sandbox", "read-only", "prompt"]
    assert with_codex_sandbox_bypass(argv) == argv


def test_bypass_inert_without_exec_subcommand():
    argv = ["codex", "login"]
    assert with_codex_sandbox_bypass(argv) == argv


def test_seeded_codex_template_carries_the_bypass():
    assert _BYPASS in default_cli_template("codex_subscription")


# ── Seeded template quoting — argv-flag injection hardening ────────────────
# The opencode/aider seeds must use the shlex-quoted {{model_q}} placeholder:
# a model slug is config-controlled data and has to land as ONE argv token,
# never split into extra flags for the executor's subprocess.


@pytest.mark.parametrize("kind", ["opencode", "aider"])
def test_seeded_templates_quote_the_model_slug(kind):
    template = default_cli_template(kind)
    assert "{{model}}" not in template
    assert "{{model_q}}" in template


@pytest.mark.parametrize("kind", ["opencode", "aider"])
def test_hostile_model_slug_stays_one_argv_token(kind):
    hostile = "x --load-plugin /tmp/evil"
    rendered = _render_template(
        default_cli_template(kind), system_prompt="s", instruction="i", model=hostile
    )
    argv = shlex.split(rendered)
    assert hostile in argv          # the whole slug is a single token
    assert "--load-plugin" not in argv


@pytest.mark.parametrize("kind", ["opencode", "aider"])
def test_benign_model_slug_renders_unchanged(kind):
    rendered = _render_template(
        default_cli_template(kind),
        system_prompt="s",
        instruction="i",
        model="opensweep/Qwen3.6-35B-A3B-4bit",
    )
    assert "opensweep/Qwen3.6-35B-A3B-4bit" in shlex.split(rendered)
