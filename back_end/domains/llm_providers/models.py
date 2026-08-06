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
    #   opencode             — local `opencode` CLI driving any OpenAI-compatible
    #                          endpoint (local server or hosted API)

    base_url = StringProperty(default="")          # for API/local servers
    model = StringProperty(default="")             # model id (eg. claude-opus-4-7 or llama-3.1-70b-instruct)
    api_key_env = StringProperty(default="")       # env var the worker reads the key from
    cli_command_template = StringProperty(default="")
    # cli_command_template — for CLI-backed providers. Use the shlex-quoted
    # placeholders for untrusted text: {{instruction_q}}, {{system_prompt_q}},
    # {{model_q}}. Eg: 'claude -p {{instruction_q}} --append-system-prompt
    # {{system_prompt_q}}'. (Raw {{instruction}}/{{system_prompt}} are gone —
    # they were argv injection; {{model}}/{{working_dir}} stay for platform paths.)

    extra_args = StringProperty(default="")        # appended verbatim to CLI
    enabled = BooleanProperty(default=True)
    active = BooleanProperty(default=False)
    # Ordered fallback chain (PLATFORM_V2_DESIGN.md §8): when the active
    # provider is quota-exhausted/unusable, the next healthy enabled provider
    # is picked by ascending fallback_priority (ties broken by label).
    fallback_priority = IntegerProperty(default=100)

    # How many runs may execute on this provider at once — the provider's own
    # capacity ceiling, independent of any one campaign's `max_parallel`. The
    # campaign tick clamps its dispatch capacity to the remaining headroom
    # (llm_providers.services.capacity.provider_headroom), so a fleet of
    # campaigns cannot collectively stampede one subscription.
    #
    # This is a SOFT ceiling on how many runs we *start*.
    max_concurrent_runs = IntegerProperty(default=5)

    notes = StringProperty(default="")

    # Sensitive credential (write-only via API; DTO returns a `has_credential_secret`
    # bool instead of the actual value). Interpretation depends on `kind`:
    #   claude_subscription → headless OAuth token from `claude setup-token`,
    #                         injected as CLAUDE_CODE_OAUTH_TOKEN env var.
    #   opencode            → optional API key for a hosted OpenAI-compatible
    #                         endpoint, written into the generated opencode.json.
    # Stored sealed via infrastructure/secretbox: writes go through
    # services/credentials.sealed_secret(), reads through provider_secret().
    credential_secret = StringProperty(default="")

    last_health_check_at = DateTimeProperty()
    last_health_status = StringProperty(default="unknown")  # ok | degraded | unreachable | unknown
    last_health_detail = StringProperty(default="")

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)
