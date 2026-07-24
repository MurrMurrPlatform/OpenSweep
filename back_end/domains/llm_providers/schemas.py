"""LLMProvider DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class LLMProviderKind(StrEnum):
    CLAUDE_SUBSCRIPTION = "claude_subscription"
    CODEX_SUBSCRIPTION = "codex_subscription"
    CLAUDE_API = "claude_api"
    OPENAI_API = "openai_api"
    MLX = "mlx"
    LMSTUDIO = "lmstudio"
    OLLAMA = "ollama"
    OPENCODE = "opencode"   # sst/opencode — TUI agent run headlessly via `opencode run`
    AIDER = "aider"         # aider-chat — headless via `aider --message`
    CUSTOM = "custom"


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
    notes: str = ""
    has_credential_secret: bool = False   # never returns the secret itself
    # Codex-subscription credential state (see codex token-refresh design).
    needs_reauth: bool = False             # refresh token dead — re-paste auth.json
    auth_state_uncertain: bool = False     # health unknown after an interrupted turn
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
    notes: str | None = None
    credential_secret: str | None = None  # write-only; pass empty string to clear


_CLAUDE_SETUP_STEPS = [
    "On your host (Mac), run: `claude setup-token` — it opens a browser for you to sign in to Anthropic.",
    "Copy the token it prints (starts with `sk-ant-oat...`).",
    "Paste it into the 'Credential' field below and save.",
    "OpenSweep stores the token in Neo4j and injects it as `CLAUDE_CODE_OAUTH_TOKEN` whenever it runs `claude` inside the container — no Keychain, no in-container login.",
]

_CODEX_SETUP_STEPS = [
    "On your host (Mac), make sure you've run `codex login` once (creates `~/.codex/auth.json`).",
    "Either: leave the bind-mount as-is (default — Codex auth is shared from `~/.codex/` on your host),",
    "Or: open `~/.codex/auth.json`, copy its contents, paste them into the 'Credential' field below. OpenSweep will write that JSON to a worker-private CODEX_HOME at run-time.",
    "The UI value (if set) takes precedence over the host bind-mount.",
]

_API_KEY_SETUP_STEPS_ANTHROPIC = [
    "Create an API key in console.anthropic.com → Settings → API keys.",
    "Either: paste it into the 'Credential' field (OpenSweep stores it),",
    "Or: set the env var named in 'API-key env var' on the backend container and leave Credential blank.",
]

_API_KEY_SETUP_STEPS_OPENAI = [
    "Create an API key in platform.openai.com → API keys.",
    "Either: paste it into the 'Credential' field, or set the env var named in 'API-key env var' on the backend.",
]

_LOCAL_SERVER_SETUP_STEPS = [
    "Start your local server (MLX `mlx_lm.server`, LMStudio's server tab, or `ollama serve`).",
    "From inside Docker, the host is reachable at `host.docker.internal` — point base_url at the right port (`/v1` for OpenAI-compat).",
    "Click `Check` to probe `<base_url>/models`.",
]

_OPENCODE_SETUP_STEPS = [
    "Rebuild the worker (`docker compose build opensweep_worker && docker compose up -d opensweep_worker`) so opencode is installed in-container.",
    "Set `base_url` to your local OpenAI-compatible endpoint, e.g. `http://host.docker.internal:2345/v1` for an OMLX server on host port 2345.",
    "Set `model` to `opensweep/<model-id>` — the `opensweep/` prefix matches the provider name the worker auto-generates in opencode's config from this row. Example: `opensweep/Qwen3.6-35B-A3B-4bit`.",
    "(No host opencode config needed.) OpenSweep writes a fresh opencode.json into the worker container before each invocation, derived from this row's base_url + model.",
    "Working directory: opencode is launched with `cwd` set to a sandbox clone of the local repo — opencode reads / edits files via its own tools, no need to inline samples.",
]

_AIDER_SETUP_STEPS = [
    "Install aider on your host: `pip install aider-chat` (or `pipx install aider-chat`).",
    "For local OpenAI-compat models, aider reads `OPENAI_API_BASE` and `OPENAI_API_KEY` — OpenSweep sets these from `base_url` and `credential_secret` (use any non-empty string for local).",
    "Rebuild the worker so aider is installed inside the container.",
    "`model` should be the model id your server exposes (e.g. `openai/Qwen3.6-35B-A3B-4bit`).",
]


# Picker metadata (spec: 2026-07-14-provider-connect-simplification):
#   default_label / tagline   — what the connect-dialog tile shows
#   default_base_url          — Docker-reachable default for local servers
#   default_api_key_env       — fallback env var for API kinds
#   featured                  — picker order; 0 = hidden from the UI picker
#     (the API still accepts hidden kinds — aider/custom are ops-only)
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
    LLMProviderKind.CODEX_SUBSCRIPTION: {
        "display_name": "OpenAI Codex (subscription CLI)",
        "default_label": "OpenAI Codex",
        "tagline": "Uses your ChatGPT subscription — no API bill",
        "featured": 2,
        "transport": "local CLI",
        "default_cli": (
            "codex exec --dangerously-bypass-approvals-and-sandbox "
            "--skip-git-repo-check --json {{instruction_q}}"
        ),
        "needs_api_key": False,
        "needs_base_url": False,
        "needs_credential": True,
        "credential_label": "Contents of ~/.codex/auth.json (optional — overrides bind-mount)",
        "credential_placeholder": '{"OPENAI_API_KEY": "...", "tokens": { ... }}',
        "setup_steps": _CODEX_SETUP_STEPS,
        "default_model": "gpt-5-codex",
    },
    LLMProviderKind.CLAUDE_API: {
        "display_name": "Anthropic API",
        "default_label": "Anthropic API",
        "tagline": "Pay-per-token with an Anthropic API key",
        "featured": 7,
        "default_api_key_env": "ANTHROPIC_API_KEY",
        "transport": "HTTPS",
        "default_cli": "",
        "needs_api_key": True,
        "needs_base_url": False,
        "needs_credential": True,
        "credential_label": "API key (optional — overrides env var)",
        "credential_placeholder": "sk-ant-api03-...",
        "setup_steps": _API_KEY_SETUP_STEPS_ANTHROPIC,
        "default_model": "claude-opus-4-7",
    },
    LLMProviderKind.OPENAI_API: {
        "display_name": "OpenAI API",
        "default_label": "OpenAI API",
        "tagline": "Pay-per-token with an OpenAI API key",
        "featured": 8,
        "default_api_key_env": "OPENAI_API_KEY",
        "transport": "HTTPS",
        "default_cli": "",
        "needs_api_key": True,
        "needs_base_url": False,
        "needs_credential": True,
        "credential_label": "API key (optional — overrides env var)",
        "credential_placeholder": "sk-proj-...",
        "setup_steps": _API_KEY_SETUP_STEPS_OPENAI,
        "default_model": "gpt-5",
    },
    LLMProviderKind.MLX: {
        "display_name": "MLX (Apple Silicon, local)",
        "default_label": "OMLX / MLX",
        "tagline": "Local Apple Silicon server — free and private",
        "featured": 4,
        "default_base_url": "http://host.docker.internal:2345/v1",
        "transport": "HTTPS",
        "default_cli": "",
        "needs_api_key": False,
        "needs_base_url": True,
        "needs_credential": False,
        "credential_label": "",
        "credential_placeholder": "",
        "setup_steps": _LOCAL_SERVER_SETUP_STEPS,
        "default_model": "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit",
    },
    LLMProviderKind.LMSTUDIO: {
        "display_name": "LMStudio (local OpenAI-compatible)",
        "default_label": "LM Studio",
        "tagline": "Local server — free and private",
        "featured": 5,
        "default_base_url": "http://host.docker.internal:1234/v1",
        "transport": "HTTPS",
        "default_cli": "",
        "needs_api_key": False,
        "needs_base_url": True,
        "needs_credential": False,
        "credential_label": "",
        "credential_placeholder": "",
        "setup_steps": _LOCAL_SERVER_SETUP_STEPS,
        "default_model": "qwen/qwen3-coder",
    },
    LLMProviderKind.OLLAMA: {
        "display_name": "Ollama (local)",
        "default_label": "Ollama",
        "tagline": "Local server — free and private",
        "featured": 6,
        "default_base_url": "http://host.docker.internal:11434/v1",
        "transport": "HTTPS",
        "default_cli": "",
        "needs_api_key": False,
        "needs_base_url": True,
        "needs_credential": False,
        "credential_label": "",
        "credential_placeholder": "",
        "setup_steps": _LOCAL_SERVER_SETUP_STEPS,
        "default_model": "qwen2.5-coder:14b",
    },
    LLMProviderKind.OPENCODE: {
        "display_name": "opencode (sst, headless agent)",
        "default_label": "opencode",
        "tagline": "Headless coding agent on your local model",
        "featured": 3,
        "default_base_url": "http://host.docker.internal:2345/v1",
        "transport": "local CLI + cwd",
        # `{{instruction_q}}` is the rendered prompt; opencode reads files in cwd itself.
        # Override the template in the UI if you want a non-default model flag etc.
        # `{{model_q}}` (shlex-quoted): the model slug is config-controlled data,
        # so it must land as ONE argv token — raw {{model}} would let a slug
        # like "x --flag" inject flags into the CLI invocation.
        "default_cli": 'opencode run -m {{model_q}} {{instruction_q}}',
        "needs_api_key": False,
        "needs_base_url": True,
        "needs_credential": False,
        "credential_label": "",
        "credential_placeholder": "",
        "setup_steps": _OPENCODE_SETUP_STEPS,
        # `opensweep/` matches the auto-generated opencode provider name (see
        # llm_executor._prepare_opencode_config).
        "default_model": "opensweep/Qwen3.6-35B-A3B-4bit",
    },
    LLMProviderKind.AIDER: {
        "display_name": "aider (headless coding agent)",
        "default_label": "aider",
        "tagline": "",
        "featured": 0,  # ops-only — hidden from the UI picker
        "default_base_url": "http://host.docker.internal:2345/v1",
        "transport": "local CLI + cwd",
        # --yes-always to skip every interactive prompt; --no-auto-commits so we
        # control the commit boundary; --no-pretty to keep stdout machine-readable.
        # {{model_q}} keeps the model slug a single argv token (no flag injection).
        "default_cli": (
            'aider --no-auto-commits --no-pretty --yes-always '
            '--model {{model_q}} --message {{instruction_q}}'
        ),
        "needs_api_key": False,
        "needs_base_url": True,
        "needs_credential": True,
        "credential_label": "OPENAI_API_KEY value (any non-empty string for local)",
        "credential_placeholder": "any-non-empty-string",
        "setup_steps": _AIDER_SETUP_STEPS,
        "default_model": "openai/Qwen3.6-35B-A3B-4bit",
    },
    LLMProviderKind.CUSTOM: {
        "display_name": "Custom",
        "default_label": "Custom",
        "tagline": "",
        "featured": 0,  # ops-only — hidden from the UI picker
        "transport": "varies",
        "default_cli": "",
        "needs_api_key": False,
        "needs_base_url": False,
        "needs_credential": False,
        "credential_label": "Credential (free-form)",
        "credential_placeholder": "",
        "setup_steps": [],
        "default_model": "",
    },
}


def kind_meta(kind: str | LLMProviderKind) -> dict:
    """The catalog entry for a kind ({} for unknown kinds)."""
    try:
        return KIND_CATALOG[LLMProviderKind(kind)]
    except ValueError:
        return {}


def default_cli_template(kind: str | LLMProviderKind) -> str:
    """The platform-owned CLI template for a kind ("" for HTTP/custom kinds).

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
