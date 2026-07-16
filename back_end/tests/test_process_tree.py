"""Process-group spawn/kill helpers (infrastructure/process_tree.py).

Agent CLI subprocesses (claude/codex/opencode) spawn their own children —
`npx`/`mcp-remote` bridges, `codebase-memory-mcp`, Bash-tool commands. A
plain proc.kill() signals only the direct child and orphans that tree; the
orphaned MCP bridges keep (re)connecting to the backend's SSE mount forever,
pinning accepted-socket fds until the process hits EMFILE ("[Errno 24] Too
many open files"). The helpers spawn agent CLIs as process-group leaders and
signal the whole group.
"""

import asyncio
import os
import signal

import pytest

from infrastructure.process_tree import kill_tree, process_group_kwargs, terminate_tree

# A shell that starts a long-lived grandchild, prints its pid, then waits —
# the same shape as an agent CLI holding an MCP bridge child.
_SHELL_WITH_CHILD = "sleep 300 & echo $!; wait"


async def _spawn_shell_with_child(**kwargs) -> tuple[asyncio.subprocess.Process, int]:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _SHELL_WITH_CHILD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **kwargs,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    return proc, int(line.strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; reaping is the group leader's business.
    return True


async def _assert_pid_gone(pid: int) -> None:
    for _ in range(40):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.05)
    os.kill(pid, signal.SIGKILL)  # don't leak the sleep past the test
    pytest.fail(f"grandchild {pid} survived the tree kill")


def test_process_group_kwargs_requests_new_session():
    assert process_group_kwargs() == {"start_new_session": True}


async def test_kill_tree_kills_grandchildren():
    proc, child_pid = await _spawn_shell_with_child(**process_group_kwargs())
    assert _pid_alive(child_pid)
    kill_tree(proc)
    await asyncio.wait_for(proc.wait(), timeout=10)
    await _assert_pid_gone(child_pid)


async def test_terminate_tree_terminates_grandchildren():
    proc, child_pid = await _spawn_shell_with_child(**process_group_kwargs())
    terminate_tree(proc)
    await asyncio.wait_for(proc.wait(), timeout=10)
    await _assert_pid_gone(child_pid)


async def test_kill_tree_without_own_group_only_kills_direct_child():
    """A process sharing OUR group (spawned without process_group_kwargs)
    must never be group-signalled — that would kill the test runner / the
    backend itself. The helper falls back to a direct kill."""
    proc, child_pid = await _spawn_shell_with_child()
    assert os.getpgid(proc.pid) == os.getpgid(0)
    try:
        kill_tree(proc)
        # Poll returncode instead of proc.wait(): the surviving grandchild
        # holds the stdout pipe open, and asyncio's wait() also waits for
        # pipe EOF (exactly how an orphaned tree wedges a turn in prod).
        for _ in range(100):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        assert proc.returncode == -signal.SIGKILL
        # Reaching this line at all proves the group wasn't signalled.
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)  # clean up the orphaned sleep
        except ProcessLookupError:
            pass


async def test_kill_tree_on_exited_process_is_quiet():
    proc = await asyncio.create_subprocess_exec("true", **process_group_kwargs())
    await proc.wait()
    kill_tree(proc)  # must not raise
    terminate_tree(proc)  # must not raise
