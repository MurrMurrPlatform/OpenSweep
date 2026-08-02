"""Two provider kinds: Claude Code + opencode (2026-08-02).

Every run now goes through a coding-agent harness. The codex_subscription
kind (and its credential-lease/app-server machinery), the aider kind (never
dispatchable), and the harness-less HTTP kinds (claude_api / openai_api /
mlx / lmstudio / ollama / custom — the removed internal_llm executor) are
gone; local servers and hosted OpenAI-compatible APIs are reached through
opencode endpoint presets instead.

House style: no backward compatibility — provider rows of removed kinds are
deleted (orgs reconnect through the new dialog; the dev stack reseeds). The
codex-only credential-lifecycle properties and the never-consulted
ScheduledAgent.default_executor (stamped by m0008) are dropped. Historical
Run.executor values are deliberately NOT rewritten — the DTO layer falls back
to `manual` for display, and rewriting would lie about what ran.

DOWN is intentionally empty: deleted provider rows are unrecoverable, and
the removed properties defaulted to inert values (m0011 precedent).
"""

VERSION = 23
NAME = "two-provider-kinds"

SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []

UP: list[str] = [
    "MATCH (p:LLMProvider) "
    "WHERE p.kind IN ['codex_subscription', 'claude_api', 'openai_api', "
    "'mlx', 'lmstudio', 'ollama', 'aider', 'custom'] "
    "DETACH DELETE p",
    "MATCH (p:LLMProvider) "
    "REMOVE p.needs_reauth, p.auth_state_uncertain, p.credential_revision",
    "MATCH (s:ScheduledAgent) REMOVE s.default_executor",
]
DOWN: list[str] = []
