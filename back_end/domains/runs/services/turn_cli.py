"""CLI turn construction for follow-up turns (PLATFORM_V3_DESIGN.md §6).

Process model — turn-based, not PTY (deliberate simplification that matches
how subscription CLIs meter usage):

- claude: each message runs ONE `claude -p <msg> --output-format stream-json
  --verbose` subprocess in the run's workspace cwd. The first turn runs
  WITHOUT `--resume`; the stream's init/result events carry `session_id`,
  which we persist as `Run.cli_session_id` and pass as `--resume <id>` on
  every later turn. Interrupt = kill the current subprocess; the run (and
  claude's own resume state) survives. Workspace recreation clears the
  resume token — the next turn reseeds from the transcript tail.

- codex: `codex exec` has no resume, so each turn re-sends a capped
  (~8k chars) transcript tail as inline context.

Everything here is pure (argv/prompt builders, line meta extraction) so it
stays unit-testable without subprocesses or Neo4j; spawning lives in
turn_service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

CODEX_CONTEXT_CAP = 8_000  # chars of transcript tail per codex turn

# Interrupt: SIGTERM first, SIGKILL after this grace period.
INTERRUPT_GRACE_SECONDS = 5.0

# Safety net so a hung CLI can never wedge a run in `running` forever.
TURN_TIMEOUT_SECONDS = 3600


# ── Command construction ─────────────────────────────────────────────────────


def build_claude_turn_argv(
    *,
    text: str,
    cli_session_id: str = "",
    mcp_config_path: str = "",
    model: str = "",
    system_prompt: str = "",
) -> list[str]:
    """One claude turn. First turn: no --resume (the stream yields the new
    session_id); later turns: --resume <cli_session_id>.

    stream-json + --verbose so every stdout line is a JSON event we can
    translate into transcript events; --include-partial-messages adds
    stream_event lines with token-level text deltas so watchers see the
    message grow as it is generated (works the same under subscription
    OAuth). bypassPermissions matches the executor adapters — the CLI always
    runs inside a disposable sandbox clone.
    """
    argv = [
        "claude",
        "-p",
        text,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        "bypassPermissions",
    ]
    if mcp_config_path:
        argv += ["--mcp-config", mcp_config_path]
    if model:
        argv += ["--model", model]
    if system_prompt:
        # Injected every turn (not just the first): the linked-entity briefing
        # must survive CLI session rotation and --resume.
        argv += ["--append-system-prompt", system_prompt]
    if cli_session_id:
        argv += ["--resume", cli_session_id]
    return argv


def build_codex_prompt(
    text: str, entries: list[dict], cap: int = CODEX_CONTEXT_CAP, system_prompt: str = ""
) -> str:
    """codex has no --resume: prepend the briefing + a capped transcript tail."""
    preamble = f"{system_prompt.strip()}\n\n" if system_prompt.strip() else ""
    if not entries:
        return f"{preamble}User message:\n{text}" if preamble else text
    lines = [f"{e.get('role', '?')}: {e.get('content', '')}" for e in entries]
    context = "\n".join(lines)
    if len(context) > cap:
        context = context[-cap:]
        first_newline = context.find("\n")
        if first_newline != -1:
            context = context[first_newline + 1 :]
    return (
        f"{preamble}"
        "Conversation so far (context only — this CLI has no session resume):\n"
        f"{context}\n\n"
        f"User message:\n{text}"
    )


def build_codex_turn_argv(
    *, prompt: str, model: str = "", config_overrides: list[str] | None = None
) -> list[str]:
    """`config_overrides` are `-c key=value` TOML overrides — how per-run MCP
    servers (the code graph) reach codex, which has no --mcp-config flag."""
    from domains.llm_providers.services.llm_executor import with_codex_sandbox_bypass

    # Bypass codex's OS sandbox + approval prompts — OpenSweep already isolates
    # the turn (same flag/rationale as the run path).
    argv = with_codex_sandbox_bypass(["codex", "exec", "--skip-git-repo-check", "--json"])
    for override in config_overrides or []:
        argv += ["-c", override]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    return argv


# ── Stream meta extraction ───────────────────────────────────────────────────


@dataclass
class StreamMeta:
    """Turn-level metadata one stdout line contributed (content events come
    from the shared stream translators)."""

    session_id: str = ""
    is_result: bool = False
    result_text: str | None = None


def extract_claude_meta(line: str) -> StreamMeta:
    meta = StreamMeta()
    s = (line or "").strip()
    if not s:
        return meta
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return meta
    if not isinstance(obj, dict):
        return meta
    sid = obj.get("session_id")
    if isinstance(sid, str):
        meta.session_id = sid
    if obj.get("type") == "result":
        meta.is_result = True
        result = obj.get("result")
        if isinstance(result, str):
            meta.result_text = result
    return meta


def parse_codex_deltas(line: str) -> list[str]:
    """Agent-message text from one `codex exec --json` stdout line
    (best-effort across the two known event shapes)."""
    s = (line or "").strip()
    if not s:
        return []
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    deltas: list[str] = []
    msg = obj.get("msg")
    if isinstance(msg, dict) and msg.get("type") == "agent_message":
        text = msg.get("message")
        if isinstance(text, str) and text:
            deltas.append(text)
    item = obj.get("item")
    if (
        obj.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "agent_message"
    ):
        text = item.get("text")
        if isinstance(text, str) and text:
            deltas.append(text)
    return deltas


# ── Env composition ──────────────────────────────────────────────────────────


def codex_turn_env(provider, *, run_uid: str = "") -> dict[str, str]:
    """Env for a codex turn: the explicit allowlist (same rule as
    `mcp_bridge.claude_env` — platform secrets like GITHUB_TOKEN,
    NEO4J_PASSWORD, OPENSWEEP_AUTH_TOKEN must never reach the agent), plus the
    provider's runtime credential plumbing (worker-private CODEX_HOME when a
    UI-stored auth.json exists)."""
    from domains.executors.agent_env import build_agent_env
    from domains.llm_providers.services.runtime_env import apply_runtime_to_env, build_runtime

    runtime = build_runtime(provider)
    env = build_agent_env(run_uid=run_uid, extra=runtime.env_vars)
    return apply_runtime_to_env(runtime, env)
