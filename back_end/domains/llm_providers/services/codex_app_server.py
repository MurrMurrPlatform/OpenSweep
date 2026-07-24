"""Stdio JSON-RPC 2.0 client for `codex app-server` (newline-delimited JSON).
Protocol verified in docs/superpowers/spikes/2026-07-24-codex-app-server.md.
Transport only — no OpenSweep domain knowledge."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from infrastructure.process_tree import kill_tree, process_group_kwargs


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
        self._reader = asyncio.create_task(self._read_loop())

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

    async def _read_loop(self):
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(AppServerError(-1, "app-server closed"))
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
        await self._write(msg)
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

    async def close(self) -> None:
        self._reader.cancel()
        try:
            kill_tree(self._proc)
            await self._proc.wait()
        except Exception:  # noqa: BLE001
            pass
