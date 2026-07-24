"""Stdio JSON-RPC 2.0 client for `codex app-server` (newline-delimited JSON).
Protocol verified in docs/superpowers/spikes/2026-07-24-codex-app-server.md.
Transport only — no OpenSweep domain knowledge."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from infrastructure.process_tree import kill_tree, process_group_kwargs
from logging_config import logger


@dataclass
class TurnResult:
    text: str = ""
    usage: dict = field(default_factory=dict)
    error: str | None = None


class AppServerError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"app-server error {code}: {message}")
        self.code = code
        self.message = message


class AppServerClient:
    def __init__(self, proc):
        self._proc = proc
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[Callable[[dict], None]] = []
        # Every in-flight turn's inbox. The read loop only ever `put_nowait`s
        # into these, so a slow/blocking consumer can never stall the loop for
        # the OTHER threads sharing this process — and an EOF can wake them all.
        self._inflight: set[asyncio.Queue] = set()
        self._closed = False
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_drainer = asyncio.create_task(self._drain_stderr())

    @property
    def alive(self) -> bool:
        """False once the server exited or the read loop hit EOF — the registry
        recycles a dead session instead of handing it to a new run."""
        return not self._closed and self._proc.returncode is None

    @classmethod
    async def spawn(cls, *, argv: list[str], env: dict, cwd: str | None = None) -> "AppServerClient":
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd, limit=16 * 1024 * 1024,
            **process_group_kwargs(),
        )
        return cls(proc)

    def on_notification(self, handler: Callable[[dict], None]) -> None:
        self._handlers.append(handler)

    def _teardown(self, reason: str) -> None:
        """Shared EOF/error teardown. Fail pending REQUESTS and also wake every
        in-flight TURN — otherwise a turn started with no wall ceiling would
        await a completion that can never come. Used both when the reader sees
        EOF and when it dies on an unexpected exception (see `_read_loop`), and
        by `close()` — a caller awaiting `request(...)` must not hang once the
        reader is about to be cancelled and can never resolve it.
        """
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AppServerError(-1, reason))
        self._pending.clear()
        for q in list(self._inflight):
            q.put_nowait(("error", reason))

    async def _drain_stderr(self) -> None:
        """Nobody else reads codex's stderr. Once the child writes ~64KB
        without a reader, the kernel pipe buffer fills and codex blocks
        forever on write(2) — `alive` would still report True, so the
        registry keeps handing out a session that can never make progress.
        Just log lines (bounded, one at a time) and let them go."""
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    logger.debug(f"codex app-server stderr: {text}", extra={"tag": "codex"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — draining must never crash the client
            return

    async def _read_loop(self):
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    self._teardown("app-server closed")
                    return
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in obj and ("result" in obj or "error" in obj):
                    fut = self._pending.pop(obj["id"], None)
                    if fut and not fut.done():
                        fut.set_result(obj)
                else:
                    for h in self._handlers:
                        try:
                            h(obj)
                        except Exception:  # noqa: BLE001 — a handler must not kill the loop
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — an oversized line (readline's 16MB
            # limit) or a malformed message (e.g. unhashable "id") must not kill the
            # reader silently and leave `alive` lying; treat it like EOF.
            logger.warning(f"codex app-server: read loop error: {exc}", extra={"tag": "codex"})
            self._teardown("app-server closed")

    async def _write(self, msg: dict) -> None:
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut          # register BEFORE writing
        msg: dict = {"method": method, "id": mid}
        if params is not None:
            msg["params"] = params
        try:
            await self._write(msg)
        except Exception:
            self._pending.pop(mid, None)  # never sent — nothing will ever resolve it
            raise
        resp = await fut
        if "error" in resp:
            err = resp["error"]
            raise AppServerError(int(err.get("code", -1)), str(err.get("message", "")))
        return resp.get("result") or {}

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def initialize(self, *, name: str = "opensweep", version: str = "0.1.0") -> dict:
        result = await self.request("initialize", {
            "clientInfo": {"name": name, "version": version},
            "capabilities": {"experimentalApi": True},
        })
        await self.notify("initialized")
        return result

    async def start_thread(self, *, cwd: str, sandbox: str = "danger-full-access",
                           approval: str = "never", model: str = "",
                           config: dict | None = None) -> str:
        params: dict = {"cwd": cwd, "sandbox": sandbox, "approvalPolicy": approval}
        if model:
            params["model"] = model
        if config:
            params["config"] = config
        result = await self.request("thread/start", params)
        return (result.get("thread") or {}).get("id") or ""

    async def run_turn(self, *, thread_id: str, text: str, model: str = "",
                       on_delta: Callable[[str], None] | None = None,
                       timeout_s: float | None = None) -> TurnResult:
        """Run one turn to completion on an existing thread.

        The notification handler does nothing but `put_nowait` onto this turn's
        own queue; the draining happens here, on the caller's task. That keeps
        `on_delta` (which persists transcript events — real I/O) OFF the shared
        read loop, so one slow consumer cannot stall or drop deltas for the
        other turns running concurrently on this same app-server.
        """
        q: asyncio.Queue = asyncio.Queue()

        def handle(obj: dict):
            m = obj.get("method"); p = obj.get("params") or {}
            tid = p.get("threadId")
            if m in ("error", "thread/realtimeError"):
                if tid in (thread_id, None):          # thread-scoped OR global error
                    q.put_nowait(("error", json.dumps(p)[:500]))
                return
            if tid != thread_id:                       # strict: ignore other threads
                return
            if m == "item/agentMessage/delta":
                d = p.get("delta") or ""
                if d:
                    q.put_nowait(("delta", d))
            elif m == "turn/completed":
                q.put_nowait(("done", p.get("usage") or {}))

        parts: list[str] = []
        usage: dict = {}
        error: str | None = None

        self.on_notification(handle)
        self._inflight.add(q)
        try:
            params: dict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
            if model:
                params["model"] = model
            await self.request("turn/start", params)

            loop = asyncio.get_running_loop()
            deadline = None if timeout_s is None else loop.time() + timeout_s
            while True:
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    error = f"turn timed out after {timeout_s}s"
                    break
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=remaining)
                except TimeoutError:
                    error = f"turn timed out after {timeout_s}s"
                    break
                if kind == "delta":
                    parts.append(payload)
                    if on_delta:
                        try:
                            on_delta(payload)
                        except Exception as exc:  # noqa: BLE001 — never fail a turn on a consumer error
                            logger.warning(f"codex app-server: delta consumer raised: {exc}",
                                           extra={"tag": "codex"})
                elif kind == "done":
                    usage = payload
                    break
                else:  # "error"
                    error = payload
                    break
        finally:
            self._inflight.discard(q)
            with contextlib.suppress(ValueError):
                self._handlers.remove(handle)
        return TurnResult(text="".join(parts), usage=usage, error=error)

    async def close(self) -> None:
        # Fail pending REQUESTS and wake in-flight TURNS before the reader is
        # cancelled — it's the only thing that would otherwise ever resolve
        # them, so a caller awaiting `request(...)` (e.g. inside `start_thread`)
        # would hang forever past this point.
        self._teardown("app-server closed")
        self._reader.cancel()
        self._stderr_drainer.cancel()
        try:
            kill_tree(self._proc)
            await self._proc.wait()
        except Exception:  # noqa: BLE001
            pass
