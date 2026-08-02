"""Argv-injection guards: git refs and CLI command templates.

- A branch name reaching `git checkout`/`git fetch` must not be parseable as an
  option (leading '-') or escape a pathspec ('..').
- The CLI command template must not expose a RAW {{instruction}}/{{system_prompt}}
  variant — those carry LLM/repo text into a shlex.split argv.
"""

import pytest
from fastapi import HTTPException

from domains.execution.services.sandbox_service import _assert_safe_git_ref
from domains.llm_providers.services.llm_executor import _render_template


@pytest.mark.parametrize("bad", ["--upload-pack=/x", "-x", "a..b", "", "  ", "a b", "a;b"])
def test_unsafe_git_ref_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        _assert_safe_git_ref(bad, field="source_branch")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("good", ["main", "opensweep/ab12cd34-fix", "release-1.2", "feature/x_y"])
def test_safe_git_ref_allowed(good):
    _assert_safe_git_ref(good, field="source_branch")  # no raise


def test_template_quotes_untrusted_and_drops_raw_variants():
    tpl = "run {{instruction_q}} --sys {{system_prompt_q}} -m {{model}}"
    out = _render_template(
        tpl, system_prompt="be nice", instruction="do; rm -rf /", model="gpt-x"
    )
    # Untrusted text is shlex-quoted (the injection can't break out).
    assert "'do; rm -rf /'" in out
    assert "'be nice'" in out
    # Platform-set {{model}} stays raw.
    assert "-m gpt-x" in out


def test_raw_untrusted_placeholder_is_not_substituted():
    # A template using the removed raw form leaves the literal placeholder
    # (an obvious failure) rather than injecting the untrusted text.
    out = _render_template(
        "run {{instruction}}", system_prompt="", instruction="$(evil)", model="m"
    )
    assert "$(evil)" not in out
    assert "{{instruction}}" in out
