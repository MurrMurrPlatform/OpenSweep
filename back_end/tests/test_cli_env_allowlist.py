"""_build_cli_env must be an allowlist, never an os.environ copy (§6/§13).

Agent CLIs (codex/opencode/aider/claude, local internal-LLM providers) run
repo code with tool access, so the platform's secrets (NEO4J_PASSWORD,
OPENSWEEP_AUTH_TOKEN, GITHUB_TOKEN, …) must never reach the subprocess env.
Only `agent_env.AGENT_ENV_ALLOWLIST` vars plus deliberately-passed
credentials may appear.
"""

from types import SimpleNamespace

import pytest

from domains.executors.agent_env import AGENT_ENV_ALLOWLIST
from domains.llm_providers.services.llm_executor import _build_cli_env

_PLATFORM_SECRETS = {
    "NEO4J_PASSWORD": "db-secret",
    "OPENSWEEP_AUTH_TOKEN": "platform-token",
    "GITHUB_TOKEN": "gh-secret",
    "GITHUB_WEBHOOK_SECRET": "hook-secret",
    "OPENSWEEP_SECRETS_KEY": "seal-key",
}


def _provider(kind, **overrides):
    base = dict(
        uid="prov-1",
        kind=kind,
        credential_secret="",
        api_key_env="",
        base_url="",
        model="",
        label="test",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def poisoned_environ(monkeypatch):
    for name, value in _PLATFORM_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _assert_no_platform_secrets(env):
    for name, value in _PLATFORM_SECRETS.items():
        assert name not in env, f"{name} leaked into the agent env"
        assert value not in env.values()


@pytest.mark.parametrize("kind", ["codex_subscription", "opencode", "aider", "custom"])
def test_no_platform_secret_reaches_the_child(poisoned_environ, kind):
    env = _build_cli_env(_provider(kind), run_uid="run-1")
    _assert_no_platform_secrets(env)


def test_only_allowlisted_platform_vars_are_inherited(poisoned_environ):
    env = _build_cli_env(_provider("codex_subscription"), run_uid="run-1")
    inherited = set(env) - {"IS_SANDBOX", "OPENSWEEP_RUN_UID"}
    assert inherited <= set(AGENT_ENV_ALLOWLIST)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/root"
    assert env["IS_SANDBOX"] == "1"
    assert env["OPENSWEEP_RUN_UID"] == "run-1"


def test_claude_subscription_passes_its_oauth_token_only(poisoned_environ):
    env = _build_cli_env(
        _provider("claude_subscription", credential_secret="sk-ant-oat-x"), run_uid="run-1"
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-x"
    assert env["IS_SANDBOX"] == "1"
    _assert_no_platform_secrets(env)


def test_aider_gets_openai_compat_vars(poisoned_environ):
    env = _build_cli_env(
        _provider("aider", base_url="http://host:2345/v1", credential_secret="k"),
        run_uid="run-1",
    )
    assert env["OPENAI_API_BASE"] == "http://host:2345/v1"
    assert env["OPENAI_BASE_URL"] == "http://host:2345/v1"
    assert env["OPENAI_API_KEY"] == "k"
    _assert_no_platform_secrets(env)


def test_opencode_gets_placeholder_key(poisoned_environ):
    # base_url empty → no generated config, but the SDK placeholder key stays.
    env = _build_cli_env(_provider("opencode"), run_uid="run-1")
    assert env["OPENAI_API_KEY"] == "local-dev"
    _assert_no_platform_secrets(env)


def test_api_key_env_is_a_named_passthrough(poisoned_environ, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "operator-set")
    env = _build_cli_env(
        _provider("codex_subscription", api_key_env="MY_PROVIDER_KEY"), run_uid="run-1"
    )
    assert env["MY_PROVIDER_KEY"] == "operator-set"
    _assert_no_platform_secrets(env)
