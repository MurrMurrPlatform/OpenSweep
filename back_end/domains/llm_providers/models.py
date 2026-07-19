"""LLMProvider — a 'where do I run the LLM' configuration record.

One LLMProvider can back many Agents. The provider holds *transport* details
(CLI command, API base URL, model id, env-var name to read the key from), while
an Agent holds *behaviour* (role, system_prompt, instruction_template).
"""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    IntegerProperty,
    StringProperty,
)


class LLMProvider(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    # Tenancy scope: ALWAYS the owning org's uid (managed by that org's
    # admins, holding THEIR credentials/API tokens). "" is legacy-unowned
    # data only — unusable and unmanageable; migration m0003 (and
    # migrate_tenancy) stamp such rows to the local org at startup.
    org_uid = StringProperty(default="", index=True)
    label = StringProperty(required=True)  # human name shown in UI
    kind = StringProperty(required=True)
    # kind in:
    #   claude_subscription  — local `claude` CLI (Anthropic subscription)
    #   codex_subscription   — local `codex` CLI (OpenAI subscription)
    #   claude_api           — direct Anthropic API
    #   openai_api           — direct OpenAI API
    #   mlx                  — Apple-Silicon mlx-lm server
    #   lmstudio             — LMStudio local OpenAI-compatible endpoint
    #   ollama               — Ollama local endpoint
    #   custom               — anything else (uses cli_command_template + env_vars)

    base_url = StringProperty(default="")          # for API/local servers
    model = StringProperty(default="")             # model id (eg. claude-opus-4-7 or llama-3.1-70b-instruct)
    api_key_env = StringProperty(default="")       # env var the worker reads the key from
    cli_command_template = StringProperty(default="")
    # cli_command_template — for CLI-backed providers. Available placeholders:
    #   {{system_prompt}}, {{instruction}}, {{model}}.
    # Eg: 'claude -p "{{instruction}}" --append-system-prompt "{{system_prompt}}"'

    extra_args = StringProperty(default="")        # appended verbatim to CLI
    enabled = BooleanProperty(default=True)
    active = BooleanProperty(default=False)
    # Ordered fallback chain (PLATFORM_V2_DESIGN.md §8): when the active
    # provider is quota-exhausted/unusable, the next healthy enabled provider
    # is picked by ascending fallback_priority (ties broken by label).
    fallback_priority = IntegerProperty(default=100)
    notes = StringProperty(default="")

    # Sensitive credential (write-only via API; DTO returns a `has_credential_secret`
    # bool instead of the actual value). Interpretation depends on `kind`:
    #   claude_subscription → headless OAuth token from `claude setup-token`,
    #                         injected as CLAUDE_CODE_OAUTH_TOKEN env var.
    #   codex_subscription  → full ~/.codex/auth.json blob, written to a worker-private
    #                         CODEX_HOME before each invocation.
    #   *_api               → optional override for the API key (instead of api_key_env).
    # TODO(encryption-at-rest): wrap this in a KMS-backed seal before production use.
    credential_secret = StringProperty(default="")

    last_health_check_at = DateTimeProperty()
    last_health_status = StringProperty(default="unknown")  # ok | degraded | unreachable | unknown
    last_health_detail = StringProperty(default="")

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)
