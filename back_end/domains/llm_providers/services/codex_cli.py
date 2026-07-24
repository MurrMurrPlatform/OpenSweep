"""Single home for launching the codex CLI — shared by both orchestrators.

The interactive **turn** path (`runs/services/turn_service` + `turn_cli`) and the
one-shot **run** path (`executors/cli_tracking` → `llm_executor`) compose codex
through these primitives, so "how to run codex" lives in ONE place. The
orchestrators stay separate on purpose — multi-turn chat vs. one-shot analysis
run with envelope/continuation/quota — but the codex CLI adapter is shared, so a
codex-specific fix lands once instead of twice.

What lives here: argv assembly (base + `-c` MCP overrides + sandbox/approval
bypass + model) and the `exec --json` event parsing. The other two codex
concerns are already single-source elsewhere and are re-exported for discovery:
credential seeding (`runtime_env.build_runtime` / `apply_runtime_to_env`) and the
per-subscription lease + rotation write-back (`codex_credential.codex_credential_txn`).
"""

from __future__ import annotations

import json
import os

from domains.llm_providers.services.codex_app_server_registry import REGISTRY
from domains.executors.mcp_bridge import codex_mcp_config_object

# ── argv assembly ─────────────────────────────────────────────────────────────

def app_server_enabled(provider) -> bool:
    if os.environ.get("OPENSWEEP_CODEX_APP_SERVER") == "1":
        return True
    extra = getattr(provider, "extra_args", "") or ""
    try:
        return bool(json.loads(extra).get("app_server")) if extra.strip().startswith("{") else False
    except Exception:  # noqa: BLE001
        return False


async def run_via_app_server(provider, *, instruction: str, working_dir: str, run_uid: str,
                             model: str = "", on_delta=None, timeout_s=None):
    """Run one codex turn through the per-subscription app-server. Returns a
    codex_app_server.TurnResult. Streams deltas via on_delta."""
    client = await REGISTRY.acquire(provider)
    config = codex_mcp_config_object(run_uid=run_uid, workspace_path=working_dir)
    thread_id = await client.start_thread(cwd=working_dir, model=model or "", config=config)
    return await client.run_turn(thread_id=thread_id, text=instruction, model=model or "",
                                 on_delta=on_delta, timeout_s=timeout_s)


SANDBOX_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

# Any of these already in the argv means the operator picked a sandbox/approval
# policy — don't override it.
_SANDBOX_OR_APPROVAL_FLAGS = (
    SANDBOX_BYPASS_FLAG,
    "--sandbox",
    "-s",
    "--ask-for-approval",
    "-a",
    "--full-auto",
)


def base_exec_argv() -> list[str]:
    """The invariant head of every codex run: `codex exec --skip-git-repo-check
    --json`. Callers layer overrides / model / prompt on top via the helpers
    below."""
    return ["codex", "exec", "--skip-git-repo-check", "--json"]


def with_sandbox_bypass(argv: list[str]) -> list[str]:
    """Insert codex's external-sandbox bypass right after `exec`.

    OpenSweep runs every agent inside a disposable workspace clone in a
    locked-down worker container — the same rationale as claude's
    `--permission-mode bypassPermissions`. Codex otherwise spins up its OWN OS
    sandbox (landlock/seccomp/bwrap), which cannot create a user namespace in the
    worker ("No permissions to create a new namespace"), so every shell command
    dies; and its non-interactive approval policy has no one to approve, so MCP
    tool calls come back as "user cancelled MCP tool call". This one flag fixes
    both.

    Idempotent, and inert if the argv isn't `codex … exec …` or already carries a
    sandbox/approval flag (an operator template wins).
    """
    if any(flag in argv for flag in _SANDBOX_OR_APPROVAL_FLAGS):
        return argv
    return _insert_after_exec(argv, [SANDBOX_BYPASS_FLAG])


def with_mcp_overrides(argv: list[str], *, run_uid: str, working_dir: str) -> list[str]:
    """Insert `-c key=value` MCP overrides after codex's `exec` subcommand.

    codex has no `--mcp-config` flag, so the per-run MCP servers (opensweep
    platform tools + code graph) ride in as config overrides. Only fires when the
    argv is recognizably `codex … exec …`; anything else passes through untouched
    (a run without MCP beats a run that can't start).
    """
    from domains.executors.mcp_bridge import codex_mcp_overrides

    overrides = codex_mcp_overrides(run_uid=run_uid, workspace_path=working_dir)
    if not overrides:
        return argv
    flags: list[str] = []
    for override in overrides:
        flags += ["-c", override]
    return _insert_after_exec(argv, flags, warn_if_no_exec=True)


def with_model(argv: list[str], *, model: str) -> list[str]:
    """Insert `--model <model>` after `exec` unless a model flag is already set.

    The seeded codex template has no `{{model}}` placeholder, so without this the
    CLI silently runs its own default, ignoring the provider's model and any
    per-stage workflow override. The template-level guard (leave templates that
    reference `{{model}}` alone) stays with the caller.
    """
    model = (model or "").strip()
    if not model or "--model" in argv or "-m" in argv:
        return argv
    return _insert_after_exec(argv, ["--model", model])


def _insert_after_exec(argv: list[str], flags: list[str], *, warn_if_no_exec: bool = False) -> list[str]:
    try:
        at = argv.index("exec") + 1
    except ValueError:
        if warn_if_no_exec:
            from logging_config import logger

            logger.warning("codex template has no `exec` subcommand — skipping overrides")
        return argv
    return argv[:at] + flags + argv[at:]


# ── `exec --json` event parsing ───────────────────────────────────────────────


def parse_deltas(line: str) -> list[str]:
    """Agent-message text from one `codex exec --json` stdout line (best-effort
    across the two known event shapes). Raw events (thread.started, reasoning, …)
    return nothing — only agent-message text belongs in the transcript."""
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


def delta_feeder():
    """Stateful reducer over the running-total stdout of `codex exec --json`.

    The run path's on_chunk delivers the cumulative stdout each tick (the turn
    path instead reads codex line-by-line). Feed the running total; get back the
    agent-message deltas from lines that completed since the last call. A partial
    trailing line is buffered until its newline arrives. Surfacing raw events
    verbatim is what put `{"type":"thread.started"}` lines in the run view.
    """
    state = {"consumed": 0, "buf": ""}

    def feed(total: str) -> list[str]:
        delta = total[state["consumed"]:]
        if not delta:
            return []
        state["consumed"] = len(total)
        state["buf"] += delta
        *lines, state["buf"] = state["buf"].split("\n")
        out: list[str] = []
        for line in lines:
            out.extend(parse_deltas(line))
        return out

    return feed
