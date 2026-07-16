"""Process-group spawn/kill for agent CLI subprocesses.

Agent CLIs (claude/codex/opencode) spawn their own children: `npx`/
`mcp-remote` stdio↔SSE bridges, `codebase-memory-mcp`, and whatever the
agent's Bash tool starts. Signalling only the direct child (proc.kill())
orphans that tree — and orphaned MCP bridges keep (re)connecting to the
backend's /mcp/platform mount forever, pinning one accepted-socket fd each in
the backend process until it dies with "[Errno 24] Too many open files".

The contract: spawn agent CLIs with `**process_group_kwargs()` (making the
CLI a session/process-group leader) and stop them with `terminate_tree` /
`kill_tree`, which signal the whole group. Both degrade to signalling just
the direct child when the process shares our own group (spawned without the
kwargs, or non-POSIX) — a group signal there would take down this process.
"""

import asyncio
import os
import signal


def process_group_kwargs() -> dict:
    """Extra create_subprocess_exec kwargs that make the child a process-group
    leader, so *_tree() can signal its entire tree."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


def _signal_tree(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = -1
        # Never signal our own group — that kills the server itself. Only a
        # child spawned with process_group_kwargs() leads its own group.
        if pgid > 0 and pgid != os.getpgid(0):
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass  # group already gone (or unsignalable) — try the child
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass  # already exited


def terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process's whole group (graceful stop)."""
    _signal_tree(proc, signal.SIGTERM)


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the process's whole group."""
    _signal_tree(proc, signal.SIGKILL)
