"""HTTP transport payload shaping (llm_executor._run_http).

Reasoning models (gpt-5, o-series) constrain the sampling params: they take
`max_completion_tokens` rather than `max_tokens`, and only the default
temperature. Azure AI Foundry rejects both violations with HTTP 400
(`unsupported_parameter` / `unsupported_value`) rather than ignoring them, so
the shaping is a correctness requirement, not a nicety.

No network: the wire call is monkeypatched to capture the payload.
"""

import json
from types import SimpleNamespace

import pytest

from domains.llm_providers.services import llm_executor
from domains.llm_providers.services.llm_executor import (
    LLMInvocation,
    _is_reasoning_model,
    _run_http,
)


@pytest.fixture()
def captured(monkeypatch):
    """Capture the JSON body `_run_http` would send."""
    box = {}

    async def fake_blocking(url, headers, payload, inv, http_timeout):
        box["url"] = url
        box["headers"] = headers
        box["payload"] = payload
        inv.exit_code = 200

    monkeypatch.setattr(llm_executor, "_blocking_chat_completion", fake_blocking)
    return box


def _provider(model: str, extra_args: str = "") -> SimpleNamespace:
    # stream=false keeps the capture on the blocking path.
    args = json.loads(extra_args) if extra_args else {}
    args.setdefault("stream", False)
    return SimpleNamespace(
        base_url="https://res.cognitiveservices.azure.com/openai/v1",
        model=model,
        extra_args=json.dumps(args),
        credential_secret="",
        api_key_env="",
        kind="openai_api",
    )


async def _payload_for(provider, captured) -> dict:
    inv = LLMInvocation()
    await _run_http(provider, "sys", "do the thing", inv, 60)
    assert not inv.error, inv.error
    return captured["payload"]


# ── Detection ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    ["gpt-5", "gpt-5-mini", "gpt-5-mini-gambit", "o1-preview", "o3-mini", "openai/gpt-5"],
)
def test_reasoning_models_detected(model):
    assert _is_reasoning_model(model)


@pytest.mark.parametrize(
    "model",
    ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "qwen3-coder", "", "gpt-50-turbo"],
)
def test_non_reasoning_models_not_detected(model):
    """`gpt-50-turbo` guards the prefix match against a bare startswith."""
    assert not _is_reasoning_model(model)


# ── Payload shaping ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reasoning_model_uses_max_completion_tokens(captured):
    payload = await _payload_for(_provider("gpt-5-mini-gambit"), captured)

    assert payload["max_completion_tokens"] == 8192
    assert "max_tokens" not in payload
    # Any explicit temperature is a 400 on these models.
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_standard_model_keeps_max_tokens_and_temperature(captured):
    payload = await _payload_for(_provider("gpt-4.1"), captured)

    assert payload["max_tokens"] == 8192
    assert payload["temperature"] == 0.2
    assert "max_completion_tokens" not in payload


@pytest.mark.asyncio
async def test_extra_args_can_force_reasoning_for_opaque_deployment_name(captured):
    """Azure deployment names are operator-chosen, so the heuristic can miss."""
    provider = _provider("gambit-prod", '{"reasoning": true}')

    payload = await _payload_for(provider, captured)

    assert payload["max_completion_tokens"] == 8192
    assert "max_tokens" not in payload
    assert "reasoning" not in payload  # consumed, not forwarded to the API


@pytest.mark.asyncio
async def test_extra_args_can_opt_out_of_a_false_positive(captured):
    provider = _provider("gpt-5-lookalike-chat", '{"reasoning": false}')

    payload = await _payload_for(provider, captured)

    assert payload["max_tokens"] == 8192
    assert payload["temperature"] == 0.2


@pytest.mark.asyncio
async def test_explicit_temperature_is_honoured_on_reasoning_models(captured):
    """Opting in is the operator's call — we only drop the *implicit* default."""
    provider = _provider("gpt-5-mini-gambit", '{"temperature": 1}')

    payload = await _payload_for(provider, captured)

    assert payload["temperature"] == 1.0


# ── opencode generated config ──────────────────────────────────────────────
#
# Both assertions below were failures observed against a live Azure AI Foundry
# deployment before the fix: a 401 (key never sent) and, once authenticated,
# HTTP 400 `unsupported_parameter: max_tokens` from the openai-compatible
# package.


def _opencode_payload(model: str, *, secret: str = "", extra_args: str = "") -> dict:
    from unittest.mock import patch

    from domains.llm_providers.services.llm_executor import _prepare_opencode_config

    provider = SimpleNamespace(
        base_url="https://res.cognitiveservices.azure.com/openai/v1",
        model=model,
        label="Azure Foundry",
        uid="p1",
        credential_secret=secret,
        api_key_env="",
        extra_args=extra_args,
    )
    with (
        patch("domains.llm_providers.services.llm_executor.os.makedirs"),
        patch("builtins.open"),
        patch("json.dump") as dumped,
    ):
        _prepare_opencode_config(provider)
    return dumped.call_args[0][0]["provider"]["opensweep"]


def test_opencode_config_carries_the_api_key():
    """The AI SDK reads options.apiKey only — OPENAI_API_KEY in the env is
    ignored, so omitting it here is a 401 against any hosted endpoint."""
    entry = _opencode_payload("opensweep/gpt-4.1", secret="azure-key")

    assert entry["options"]["apiKey"] == "azure-key"
    assert entry["options"]["baseURL"].endswith("/openai/v1")


def test_opencode_config_omits_apikey_when_there_is_no_credential():
    """Local servers take no key; an empty apiKey would be sent as a real one."""
    entry = _opencode_payload("opensweep/qwen3-coder")

    assert "apiKey" not in entry["options"]


def test_opencode_reasoning_model_uses_first_party_openai_package():
    """`@ai-sdk/openai-compatible` hardcodes max_tokens, which gpt-5 rejects."""
    entry = _opencode_payload("opensweep/gpt-5-mini-gambit", secret="k")

    assert entry["npm"] == "@ai-sdk/openai"


def test_opencode_standard_model_keeps_the_compatible_package():
    entry = _opencode_payload("opensweep/qwen3-coder")

    assert entry["npm"] == "@ai-sdk/openai-compatible"


def test_opencode_npm_package_is_overridable():
    entry = _opencode_payload(
        "opensweep/gpt-5-mini-gambit", extra_args='{"opencode_npm": "@ai-sdk/azure"}'
    )

    assert entry["npm"] == "@ai-sdk/azure"


@pytest.mark.asyncio
async def test_url_and_auth_header(captured):
    provider = _provider("gpt-4.1")
    provider.api_key_env = "OPENSWEEP_TEST_KEY_ENV"

    inv = LLMInvocation()
    import os

    os.environ["OPENSWEEP_TEST_KEY_ENV"] = "secret-value"
    try:
        await _run_http(provider, "sys", "go", inv, 60)
    finally:
        os.environ.pop("OPENSWEEP_TEST_KEY_ENV", None)

    assert captured["url"] == (
        "https://res.cognitiveservices.azure.com/openai/v1/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer secret-value"
