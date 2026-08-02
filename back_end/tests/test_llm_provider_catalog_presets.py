"""The two-kind catalog and opencode's endpoint presets.

The connect dialog is fully catalog-driven (GET /llm-providers/catalog returns
KIND_CATALOG verbatim), so these assertions pin the shape the frontend
flattens into picker tiles.
"""

import json

from domains.llm_providers.schemas import (
    KIND_CATALOG,
    LLMProviderKind,
    kind_meta,
)


def test_catalog_has_exactly_the_two_harness_kinds():
    assert set(KIND_CATALOG) == {
        LLMProviderKind.CLAUDE_SUBSCRIPTION,
        LLMProviderKind.OPENCODE,
    }
    assert set(LLMProviderKind) == set(KIND_CATALOG)


def test_opencode_carries_the_five_endpoint_presets():
    presets = kind_meta("opencode")["endpoint_presets"]
    assert [p["key"] for p in presets] == [
        "omlx", "lmstudio", "ollama", "azure_foundry", "openai_compatible",
    ]
    for p in presets:
        assert p["label"]
        assert "needs_api_key" in p
        assert p["setup_steps"], f"preset {p['key']} has no setup steps"
        # Every preset's model keeps the opensweep/ prefix the generated
        # opencode.json resolves against (_OPENCODE_GENERATED_PROVIDER_NAME).
        assert p["default_model"].startswith("opensweep/")


def test_local_presets_need_no_key_and_point_at_docker_host():
    presets = {p["key"]: p for p in kind_meta("opencode")["endpoint_presets"]}
    for key, port in (("omlx", 2345), ("lmstudio", 1234), ("ollama", 11434)):
        p = presets[key]
        assert p["needs_api_key"] is False
        assert p["base_url"] == f"http://host.docker.internal:{port}/v1"


def test_hosted_presets_require_a_key():
    presets = {p["key"]: p for p in kind_meta("opencode")["endpoint_presets"]}
    assert presets["azure_foundry"]["needs_api_key"] is True
    assert presets["openai_compatible"]["needs_api_key"] is True


def test_azure_preset_forces_the_first_party_ai_sdk_package():
    """Azure reasoning deployments 400 on max_tokens; the preset's extra_args
    must parse to the opencode_npm override _prepare_opencode_config reads."""
    presets = {p["key"]: p for p in kind_meta("opencode")["endpoint_presets"]}
    overrides = json.loads(presets["azure_foundry"]["extra_args"])
    assert overrides == {"opencode_npm": "@ai-sdk/openai"}


def test_claude_kind_has_no_presets():
    assert "endpoint_presets" not in kind_meta("claude_subscription")
