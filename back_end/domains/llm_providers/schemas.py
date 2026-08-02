"""LLMProvider DTOs.

Two provider kinds, both coding-agent harnesses (design decision 2026-08-02:
every run goes through a harness — no raw HTTP chat-completions path):

- claude_subscription — the Claude Code CLI on a Claude subscription.
- opencode — sst/opencode driving ANY OpenAI-compatible endpoint: local
  servers (OMLX/MLX, LM Studio, Ollama) and hosted APIs (Azure AI Foundry,
  OpenRouter, …). The connect dialog surfaces these as `endpoint_presets`.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class LLMProviderKind(StrEnum):
    CLAUDE_SUBSCRIPTION = "claude_subscription"
    OPENCODE = "opencode"   # sst/opencode — TUI agent run headlessly via `opencode run`


class LLMProviderHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class LLMProviderDTO(BaseModel):
    uid: str
    # The owning org — callers only ever see their own org's providers.
    org_uid: str = ""
    label: str
    kind: LLMProviderKind
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    cli_command_template: str = ""
    extra_args: str = ""
    enabled: bool = True
    active: bool = False
    fallback_priority: int = 100  # §8 fallback chain — lower runs first
    # How many runs may execute on this provider at once (>=1). Campaign
    # dispatch clamps to the remaining headroom.
    max_concurrent_runs: int = 5
    notes: str = ""
    has_credential_secret: bool = False   # never returns the secret itself
    last_health_check_at: datetime | None = None
    last_health_status: LLMProviderHealth = LLMProviderHealth.UNKNOWN
    last_health_detail: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreateLLMProviderRequest(BaseModel):
    # Providers are always owned by the caller's org. Everything except the
    # kind is optional — empty fields fill from KIND_CATALOG defaults, so the
    # connect dialog can send as little as {kind, credential_secret}.
    label: str = ""
    kind: LLMProviderKind
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    cli_command_template: str = ""
    extra_args: str = ""
    enabled: bool = True
    active: bool = False
    fallback_priority: int = 100
    # 0/unset = the platform default for this kind (kind_meta).
    max_concurrent_runs: int = 0
    notes: str = ""
    credential_secret: str = ""  # write-only


class UpdateLLMProviderRequest(BaseModel):
    label: str | None = None
    kind: LLMProviderKind | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    cli_command_template: str | None = None
    extra_args: str | None = None
    enabled: bool | None = None
    active: bool | None = None
    fallback_priority: int | None = None
    max_concurrent_runs: int | None = None
    notes: str | None = None
    credential_secret: str | None = None  # write-only; pass empty string to clear


_CLAUDE_SETUP_STEPS = [
    "On your host (Mac), run: `claude setup-token` — it opens a browser for you to sign in to Anthropic.",
    "Copy the token it prints (starts with `sk-ant-oat...`).",
    "Paste it into the 'Credential' field below and save.",
    "OpenSweep stores the token in Neo4j and injects it as `CLAUDE_CODE_OAUTH_TOKEN` whenever it runs `claude` inside the container — no Keychain, no in-container login.",
]

_OPENCODE_SHARED_STEPS = [
    "Set `model` to `opensweep/<model-id>` — the `opensweep/` prefix matches the provider name the worker auto-generates in opencode's config from this row.",
    "(No host opencode config needed.) OpenSweep writes a fresh opencode.json into the worker container before each invocation, derived from this row's base_url + model + API key.",
    "Working directory: opencode is launched with `cwd` set to a sandbox clone of the repo — opencode reads / edits files via its own tools.",
]

_OPENCODE_LOCAL_STEPS = [
    "Start your local server and check the port in the Server URL (from inside Docker, your host is `host.docker.internal`).",
    *_OPENCODE_SHARED_STEPS,
]

_OPENCODE_AZURE_STEPS = [
    "In Azure AI Foundry, deploy a model and note the *deployment name* — that is your model id.",
    "Server URL: `https://<resource>.cognitiveservices.azure.com/openai/v1`.",
    "Paste an API key for the resource into the 'API key' field.",
    "Set `model` to `opensweep/<deployment-name>`.",
    *_OPENCODE_SHARED_STEPS[1:],
]

_OPENCODE_API_STEPS = [
    "Point the Server URL at any OpenAI-compatible endpoint (OpenRouter, a proxy, your own gateway) — include the `/v1` suffix if the service uses one.",
    "Paste the service's API key into the 'API key' field.",
    *_OPENCODE_SHARED_STEPS,
]


# Picker metadata (spec: 2026-07-14-provider-connect-simplification):
#   default_label / tagline   — what the connect-dialog tile shows
#   default_base_url          — Docker-reachable default for local servers
#   featured                  — picker order; 0 = hidden from the UI picker
#   default_max_concurrent_runs — capacity ceiling for the kind; omitted means
#     DEFAULT_MAX_CONCURRENT_RUNS.
#   endpoint_presets (opencode) — one connect-dialog tile per endpoint the
#     harness can drive; each prefills base_url/model/extra_args and carries
#     its own setup steps. `extra_args` flows into
#     llm_executor._prepare_opencode_config (e.g. the Azure preset forces the
#     first-party @ai-sdk/openai package, which sends max_completion_tokens
#     as reasoning deployments require).
KIND_CATALOG: dict[LLMProviderKind, dict] = {
    LLMProviderKind.CLAUDE_SUBSCRIPTION: {
        "display_name": "Claude Code (subscription CLI)",
        "default_label": "Claude Code",
        "tagline": "Uses your Claude subscription — no API bill",
        "featured": 1,
        "transport": "local CLI",
        # --mcp-config wires OpenSweep's MCP server into Claude so it can
        # opensweep_create_candidate / opensweep_find_similar etc. The path is a
        # per-run JSON file OpenSweep writes before invoking the CLI; the
        # `{{mcp_config_path}}` placeholder is substituted automatically.
        # --permission-mode bypassPermissions lets the CLI run its file/bash tools
        # without asking, which is what we need in a sandbox.
        # --append-system-prompt (NOT --system-prompt): the platform contract is
        # layered on top of Claude Code's own system prompt. Replacing it strips
        # the CLI's agentic scaffolding (persistence, planning, tool guidance)
        # and produces visibly shallower, shorter runs.
        "default_cli": (
            'claude -p {{instruction_q}} --append-system-prompt {{system_prompt_q}} '
            '--mcp-config {{mcp_config_path_q}} '
            '--permission-mode bypassPermissions --output-format stream-json --verbose'
        ),
        "needs_api_key": False,
        "needs_base_url": False,
        "needs_credential": True,
        "credential_label": "Headless OAuth token",
        "credential_placeholder": "sk-ant-oat...",
        "setup_steps": _CLAUDE_SETUP_STEPS,
        "default_model": "claude-opus-4-7",
    },
    LLMProviderKind.OPENCODE: {
        "display_name": "opencode (sst, headless agent)",
        "default_label": "opencode",
        "tagline": "Headless coding agent on your local model or any OpenAI-compatible API",
        "featured": 2,
        "default_base_url": "http://host.docker.internal:2345/v1",
        "transport": "local CLI + cwd",
        # `{{instruction_q}}` is the rendered prompt; opencode reads files in cwd itself.
        # `{{model_q}}` (shlex-quoted): the model slug is config-controlled data,
        # so it must land as ONE argv token — raw {{model}} would let a slug
        # like "x --flag" inject flags into the CLI invocation.
        "default_cli": 'opencode run -m {{model_q}} {{instruction_q}}',
        "needs_api_key": False,
        "needs_base_url": True,
        # A local server needs no key; a hosted OpenAI-compatible endpoint
        # (Azure Foundry, OpenRouter) answers 401 without one — the AI SDK
        # reads it from the generated config only. So: offer the field, never
        # require it (see llm_executor._prepare_opencode_config).
        "needs_credential": True,
        "credential_optional": True,
        "credential_label": "API key (leave blank for a local server)",
        "credential_placeholder": "",
        "setup_steps": _OPENCODE_LOCAL_STEPS,
        # `opensweep/` matches the auto-generated opencode provider name (see
        # llm_executor._prepare_opencode_config).
        "default_model": "opensweep/Qwen3.6-35B-A3B-4bit",
        "endpoint_presets": [
            {
                "key": "omlx",
                "label": "OMLX / MLX (Apple Silicon)",
                "tagline": "Local Apple Silicon server — free and private",
                "base_url": "http://host.docker.internal:2345/v1",
                "needs_api_key": False,
                "default_model": "opensweep/Qwen3.6-35B-A3B-4bit",
                "extra_args": "",
                "setup_steps": _OPENCODE_LOCAL_STEPS,
            },
            {
                "key": "lmstudio",
                "label": "LM Studio",
                "tagline": "Local server — free and private",
                "base_url": "http://host.docker.internal:1234/v1",
                "needs_api_key": False,
                "default_model": "opensweep/qwen/qwen3-coder",
                "extra_args": "",
                "setup_steps": _OPENCODE_LOCAL_STEPS,
            },
            {
                "key": "ollama",
                "label": "Ollama",
                "tagline": "Local server — free and private",
                "base_url": "http://host.docker.internal:11434/v1",
                "needs_api_key": False,
                "default_model": "opensweep/qwen2.5-coder:14b",
                "extra_args": "",
                "setup_steps": _OPENCODE_LOCAL_STEPS,
            },
            {
                "key": "azure_foundry",
                "label": "Azure AI Foundry",
                "tagline": "Hosted OpenAI-compatible — deployment-name models",
                "base_url": "https://<resource>.cognitiveservices.azure.com/openai/v1",
                "needs_api_key": True,
                "default_model": "opensweep/<deployment-name>",
                # First-party @ai-sdk/openai sends max_completion_tokens, which
                # Azure reasoning deployments require (the openai-compatible
                # package sends max_tokens and gets HTTP 400).
                "extra_args": '{"opencode_npm": "@ai-sdk/openai"}',
                "setup_steps": _OPENCODE_AZURE_STEPS,
            },
            {
                "key": "openai_compatible",
                "label": "OpenAI-compatible API",
                "tagline": "OpenRouter, proxies, or any /chat/completions host",
                "base_url": "",
                "needs_api_key": True,
                "default_model": "opensweep/<model-id>",
                "extra_args": "",
                "setup_steps": _OPENCODE_API_STEPS,
            },
        ],
    },
}


def kind_meta(kind: str | LLMProviderKind) -> dict:
    """The catalog entry for a kind ({} for unknown kinds)."""
    try:
        return KIND_CATALOG[LLMProviderKind(kind)]
    except ValueError:
        return {}


# Fan-out a provider is assumed to tolerate when nothing says otherwise.
DEFAULT_MAX_CONCURRENT_RUNS = 5


def default_max_concurrent_runs(kind: str | LLMProviderKind) -> int:
    """The capacity ceiling to stamp on a new row of this kind."""
    return int(
        kind_meta(kind).get("default_max_concurrent_runs")
        or DEFAULT_MAX_CONCURRENT_RUNS
    )


def default_cli_template(kind: str | LLMProviderKind) -> str:
    """The platform-owned CLI template for a kind.

    The template encodes how OpenSweep drives each CLI (flag order, MCP
    wiring), so it must never be required user input: executors and the
    provider service fall back to this whenever a row's template is empty."""
    return str(kind_meta(kind).get("default_cli") or "")


# Provider rows are stamped with the catalog default at creation, so a row
# whose template exactly matches an OLD seeded default was never human-tuned.
# Executors resolve those forward at dispatch time (no DB migration; a
# human-customised template is always preserved). Keyed by kind value.
_LEGACY_CLI_TEMPLATES: dict[str, tuple[str, ...]] = {
    LLMProviderKind.CLAUDE_SUBSCRIPTION.value: (
        # Pre-2026-07: --system-prompt REPLACED Claude Code's own system
        # prompt, stripping its agentic scaffolding.
        'claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} '
        '--mcp-config {{mcp_config_path_q}} '
        '--permission-mode bypassPermissions --output-format stream-json --verbose',
    ),
}


def effective_cli_template(kind: str | LLMProviderKind, stored: str | None) -> str:
    """The template a dispatch should actually use: the stored one, unless it
    is empty or a known legacy seeded default — both resolve to the current
    catalog default."""
    kind_value = kind.value if isinstance(kind, LLMProviderKind) else str(kind or "")
    template = (stored or "").strip()
    if not template or template in _LEGACY_CLI_TEMPLATES.get(kind_value, ()):
        return default_cli_template(kind)
    return template
