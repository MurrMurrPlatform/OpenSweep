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

- opencode: `opencode run` has no resume, so each turn re-sends a capped
  (~8k chars) transcript tail as inline context (build_transcript_prompt).

Everything here is pure (argv/prompt builders, line meta extraction) so it
stays unit-testable without subprocesses or Neo4j; spawning lives in
turn_service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

TRANSCRIPT_CONTEXT_CAP = 8_000  # chars of transcript tail per no-resume turn

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


def build_transcript_prompt(
    text: str, entries: list[dict], cap: int = TRANSCRIPT_CONTEXT_CAP, system_prompt: str = ""
) -> str:
    """No-resume CLIs (opencode): prepend the briefing + a capped transcript tail."""
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
