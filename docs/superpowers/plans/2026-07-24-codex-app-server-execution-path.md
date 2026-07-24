# Codex App-Server Local Execution Path (Phase 4a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run codex-subscription runs through a persistent, process-local `codex app-server` (one per subscription, multiplexing concurrent threads) instead of one-shot `codex exec`, so multiple runs on one ChatGPT subscription execute concurrently with no credential-rotation race — behind the existing `codex_cli` seam and a feature flag, orchestrators untouched.

**Architecture:** A stdio JSONL app-server client (spike-verified protocol) + a per-`(provider_uid, credential_revision)` registry that lazily spawns and reuses one app-server per subscription (seeding a private `CODEX_HOME` from the sealed secret once, letting the app-server own token refresh). Each run becomes one `thread/start` (cwd = the run's sandbox clone, `sandbox: danger-full-access`, per-run MCP servers via `config.mcp_servers`) + one `turn/start`; `item/agentMessage/delta` streams to the existing `append_event` pipeline; completion is detected the same way as today (`_completed_via_mcp` — codex calls the platform `complete_run` MCP tool). On this path the per-run lease and the `exec` continuation hack are redundant and are bypassed. Multi-tenant fleet placement and API-key auth are **separate follow-on plans** (see Out of Scope).

**Tech Stack:** Python 3.13, asyncio, neomodel(Neo4j), pytest (`asyncio_mode=auto`), codex-cli 0.145.0 `app-server` (experimental, JSONL over stdio).

## Global Constraints

- **Two-repo rule:** shared product code lands in `opensweep/` (public); cloud overlay merges via `git fetch upstream && git merge upstream/main`. No `if cloud:` in shared files. (`opensweep/CLAUDE.md`)
- **Protocol (spike-verified, `docs/superpowers/spikes/2026-07-24-codex-app-server.md`):** stdio = newline-delimited JSON-RPC 2.0. Handshake: request `initialize {clientInfo:{name,version}, capabilities:{experimentalApi:true}}` → result `{userAgent,codexHome,…}` → notify `initialized`. `thread/start {cwd, sandbox, approvalPolicy, model?, config?}` → `{thread:{id}}`. `turn/start {threadId, input:[{type:"text",text}], model?}` → `{turn:{id}}`, then stream `turn/started` · `item/started` · `item/agentMessage/delta {..., delta}` · `item/completed` · `thread/tokenUsage/updated` · `turn/completed {usage}`. Errors surface as `error` / `thread/realtimeError` notifications or a JSON-RPC `error` on a response.
- **Sandbox:** `sandbox: "danger-full-access"`, `approvalPolicy: "never"` — OpenSweep's container + workspace clone IS the sandbox (same rationale as the `--dangerously-bypass-approvals-and-sandbox` we ship for `exec`).
- **Credential rule (OpenAI docs):** one `auth.json` per runner, never rewrite from the seed once running (throws away refreshed tokens). The app-server is that single runner; seed its `CODEX_HOME` ONCE, persist its rotations back (reuse `codex_credential`).
- **Feature-flagged:** the app-server path is opt-in via `OPENSWEEP_CODEX_APP_SERVER=1` (env) OR provider `extra_args` `{"app_server": true}`; default OFF → today's `exec` path unchanged. Every existing codex test stays green.
- **Test venv:** run from `back_end/`: `.venv/bin/python -m pytest`.

## Out of Scope (separate follow-on plans)
- **Phase 4b (cloud overlay):** multi-node fleet placement, warm pools, idle shutdown, per-subscription admission/throttle off `account/rateLimits/updated`, cross-node routing. Builds on this plan's registry + client.
- **Phase 4c (public, small):** first-class API-key codex provider (seed an API-key `auth.json` — no OAuth, no lease, parallel by construction).
- **Retiring `_codex_delta_feeder` / `exec` entirely:** kept as the default path until 4a is proven in production.

### Phase 4b activation gates (from the 4a final whole-branch review)
Before the flag is turned ON in production, these must be addressed (the 4a code is dormant/default-OFF, so they don't block the 4a merge):
- **[FIXED in 4a, commit 915fa47]** ~~CODEX_HOME keyed by uid only → a rotated-rev server clobbers a live server's auth.json.~~ `_codex_home` is now `(uid, credential_revision)`-scoped.
- **Delta callback runs on the client read-loop** (`codex_app_server.run_turn.handle` → `on_delta` → `cli_tracking._on_delta` → `append_event`). If `append_event` blocks or raises, it stalls/silently-drops for ALL concurrent threads on that shared process. 4b: confirm `append_event` is non-blocking+total, or hand deltas off via a per-turn queue instead of calling user code on the read loop.
- **EOF doesn't fail in-flight turn futures** — only pending *request* futures are failed on read-loop EOF. A `run_turn` with `timeout_s=None` + a server crash hangs forever. The executor always passes a wall ceiling (so `timeout_s` is set), but 4b should also resolve in-flight turn `done` futures on EOF.
- **Registry lifecycle:** idle-timeout shutdown, rotation write-back (CAS-persist the app-server's refreshed auth.json to the sealed credential via `codex_credential`), and `_locks` pruning — all deferred here, required for a long-lived fleet.

---

## File Structure

- `domains/llm_providers/services/codex_app_server.py` — **NEW.** The stdio JSONL client + thread/turn lifecycle. One responsibility: speak the app-server protocol. No OpenSweep domain knowledge.
- `domains/llm_providers/services/codex_app_server_registry.py` — **NEW.** `AppServerRegistry`: lazy spawn + reuse one app-server per subscription; CODEX_HOME seeding; shutdown/rotation write-back. Owns process lifecycle.
- `domains/llm_providers/services/codex_cli.py` — **MODIFY.** Add `run_via_app_server(...)` + `app_server_enabled(provider)` (the seam both orchestrators call).
- `domains/executors/cli_tracking.py` — **MODIFY.** Route the codex dispatch through `run_via_app_server` when enabled; else unchanged `exec` path.
- `domains/executors/mcp_bridge.py` — **MODIFY (add one helper).** `codex_mcp_config_object(run_uid, workspace_path)` → the nested dict form of the existing `codex_mcp_overrides` `-c` strings, for `thread/start.config`.
- Tests: `tests/test_codex_app_server_client.py`, `tests/test_codex_app_server_registry.py`, `tests/test_codex_cli_app_server.py`, `tests/test_cli_tracking_app_server.py`, plus a fixture fake server `tests/fixtures/fake_codex_app_server.py`.

---

## Task 1: App-server JSONL client transport + handshake

**Files:**
- Create: `domains/llm_providers/services/codex_app_server.py`
- Create: `tests/fixtures/fake_codex_app_server.py`
- Test: `tests/test_codex_app_server_client.py`

**Interfaces:**
- Produces: `class AppServerClient` with `@classmethod async def spawn(cls, *, argv: list[str], env: dict, cwd: str | None = None) -> "AppServerClient"`, `async def initialize(self, *, name="opensweep", version="0.1.0") -> dict`, `async def request(self, method: str, params: dict | None = None) -> dict` (returns the JSON-RPC `result`, raises `AppServerError` on an `error`), `async def notify(self, method: str, params: dict | None = None) -> None`, `def on_notification(self, handler: Callable[[dict], None]) -> None`, `async def close(self) -> None`. Exception: `class AppServerError(Exception)` carrying `.code`, `.message`.

- [ ] **Step 1: Write the fake app-server fixture** (a stdlib script the tests spawn instead of real codex)

```python
# tests/fixtures/fake_codex_app_server.py
"""Minimal fake `codex app-server --stdio` for tests: JSONL JSON-RPC.
Responds to initialize; on thread/start returns a thread id; on turn/start
returns a turn id then emits agentMessage deltas + turn/completed. Deterministic."""
import json, sys

def send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def main():
    thread_seq = 0
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        msg = json.loads(line)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            send({"id": mid, "result": {"userAgent": "fake/0", "codexHome": params.get("_home", ""),
                                        "platformFamily": "unix", "platformOs": "test"}})
        elif method == "initialized":
            pass  # notification, no reply
        elif method == "thread/start":
            thread_seq += 1
            tid = f"thr_{thread_seq}"
            send({"id": mid, "result": {"thread": {"id": tid}}})
            send({"method": "thread/started", "params": {"thread": {"id": tid}}})
        elif method == "turn/start":
            tid = params["threadId"]
            send({"id": mid, "result": {"turn": {"id": f"turn_{tid}", "status": "inProgress"}}})
            text = params["input"][0]["text"]
            reply = f"echo:{text}"
            for ch in (reply[:3], reply[3:]):  # two deltas
                send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "delta": ch}})
            send({"method": "turn/completed", "params": {"threadId": tid, "usage": {"input_tokens": 1}}})
        elif method == "boom/error":
            send({"id": mid, "error": {"code": -32000, "message": "boom"}})
        else:
            if mid is not None:
                send({"id": mid, "result": {}})

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing client test (handshake + request/error)**

```python
# tests/test_codex_app_server_client.py
import sys
import pytest
from domains.llm_providers.services.codex_app_server import AppServerClient, AppServerError

pytestmark = pytest.mark.asyncio
_FAKE = [sys.executable, "tests/fixtures/fake_codex_app_server.py"]


async def test_initialize_handshake_returns_server_info():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        info = await c.initialize()
        assert info["platformOs"] == "test"
    finally:
        await c.close()


async def test_request_raises_appservererror_on_error_response():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        with pytest.raises(AppServerError) as exc:
            await c.request("boom/error")
        assert exc.value.code == -32000 and "boom" in exc.value.message
    finally:
        await c.close()
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_client.py -v`
Expected: FAIL — `ModuleNotFoundError: codex_app_server`.

- [ ] **Step 4: Implement the client**

```python
# domains/llm_providers/services/codex_app_server.py
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

    async def _send(self, method: str, params: dict | None, *, want_id: bool) -> int | None:
        msg: dict = {"method": method}
        mid = None
        if want_id:
            self._id += 1
            mid = self._id
            msg["id"] = mid
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        return mid

    async def request(self, method: str, params: dict | None = None) -> dict:
        mid = await self._send(method, params, want_id=True)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        resp = await fut
        if "error" in resp:
            err = resp["error"]
            raise AppServerError(int(err.get("code", -1)), str(err.get("message", "")))
        return resp.get("result") or {}

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send(method, params, want_id=False)

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_client.py -v`
Expected: PASS (2/2).

- [ ] **Step 6: Commit**

```bash
git add domains/llm_providers/services/codex_app_server.py tests/fixtures/fake_codex_app_server.py tests/test_codex_app_server_client.py
git commit -m "Add codex app-server stdio JSON-RPC client + handshake"
```

---

## Task 2: Thread + turn lifecycle (streaming)

**Files:**
- Modify: `domains/llm_providers/services/codex_app_server.py`
- Test: `tests/test_codex_app_server_client.py`

**Interfaces:**
- Consumes: `AppServerClient` (Task 1).
- Produces: `@dataclass TurnResult: text: str; usage: dict; error: str | None` and on the client: `async def start_thread(self, *, cwd: str, sandbox="danger-full-access", approval="never", model="", config: dict | None = None) -> str` (returns thread id) and `async def run_turn(self, *, thread_id: str, text: str, model="", on_delta: Callable[[str], None] | None = None, timeout_s: float | None = None) -> TurnResult`.

- [ ] **Step 1: Write the failing lifecycle test**

```python
async def test_start_thread_and_run_turn_streams_and_completes():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        tid = await c.start_thread(cwd="/tmp/x")
        deltas = []
        res = await c.run_turn(thread_id=tid, text="hi", on_delta=deltas.append)
        assert "".join(deltas) == "echo:hi"       # streamed
        assert res.text == "echo:hi" and res.error is None
        assert res.usage.get("input_tokens") == 1
    finally:
        await c.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_client.py::test_start_thread_and_run_turn_streams_and_completes -v`
Expected: FAIL — `AppServerClient has no attribute start_thread`.

- [ ] **Step 3: Implement thread/turn lifecycle**

Add to `codex_app_server.py`:

```python
from dataclasses import dataclass, field


@dataclass
class TurnResult:
    text: str = ""
    usage: dict = field(default_factory=dict)
    error: str | None = None


# --- inside AppServerClient ---

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
        done: asyncio.Future = asyncio.get_event_loop().create_future()
        parts: list[str] = []
        state: dict = {"usage": {}, "error": None}

        def handle(obj: dict):
            m = obj.get("method"); p = obj.get("params") or {}
            if p.get("threadId") not in (thread_id, None):
                return
            if m == "item/agentMessage/delta":
                d = p.get("delta") or ""
                if d:
                    parts.append(d)
                    if on_delta:
                        on_delta(d)
            elif m in ("error", "thread/realtimeError"):
                state["error"] = json.dumps(p)[:500]
                if not done.done():
                    done.set_result(True)
            elif m == "turn/completed":
                state["usage"] = p.get("usage") or {}
                if not done.done():
                    done.set_result(True)

        self.on_notification(handle)
        params: dict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        if model:
            params["model"] = model
        await self.request("turn/start", params)
        try:
            if timeout_s is None:
                await done
            else:
                await asyncio.wait_for(done, timeout=timeout_s)
        except TimeoutError:
            state["error"] = f"turn timed out after {timeout_s}s"
        finally:
            self._handlers.remove(handle)
        return TurnResult(text="".join(parts), usage=state["usage"], error=state["error"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_client.py -v`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add domains/llm_providers/services/codex_app_server.py tests/test_codex_app_server_client.py
git commit -m "Add app-server thread/turn lifecycle with streaming deltas"
```

---

## Task 3: Per-subscription app-server registry

**Files:**
- Create: `domains/llm_providers/services/codex_app_server_registry.py`
- Test: `tests/test_codex_app_server_registry.py`

**Interfaces:**
- Consumes: `AppServerClient.spawn` (Task 1); `runtime_env.build_runtime` + `apply_runtime_to_env` (existing — seed private `CODEX_HOME`/`auth.json` + set `HOME`/`CODEX_HOME`).
- Produces: `class AppServerRegistry` with `async def acquire(self, provider) -> AppServerClient` (spawns-or-reuses one initialized client per `(provider.uid, provider.credential_revision)`; concurrency-safe via an `asyncio.Lock` per key) and `async def shutdown(self, provider) -> None` / `async def shutdown_all(self) -> None`. A module-level singleton `REGISTRY = AppServerRegistry()`. Injectable launcher: `AppServerRegistry(spawn=<callable>)` defaulting to `AppServerClient.spawn` so tests pass a fake.

- [ ] **Step 1: Write the failing registry test (reuse + key on revision)**

```python
# tests/test_codex_app_server_registry.py
import pytest
from types import SimpleNamespace
from domains.llm_providers.services.codex_app_server_registry import AppServerRegistry
pytestmark = pytest.mark.asyncio


class _FakeClient:
    def __init__(self): self.initialized = 0; self.closed = False
    async def initialize(self): self.initialized += 1
    async def close(self): self.closed = True


def _provider(uid="p1", rev=0, secret="sealed-x"):
    return SimpleNamespace(uid=uid, kind="codex_subscription",
                           credential_secret=secret, credential_revision=rev)


async def test_acquire_reuses_one_server_per_subscription(monkeypatch):
    spawned = []
    async def fake_spawn(*, argv, env, cwd=None):
        c = _FakeClient(); spawned.append(c); return c
    # avoid real file writes when seeding
    import domains.llm_providers.services.codex_app_server_registry as reg
    monkeypatch.setattr(reg, "_seed_codex_home", lambda provider: {"env": {}, "cwd": None})

    r = AppServerRegistry(spawn=fake_spawn)
    p = _provider()
    a = await r.acquire(p)
    b = await r.acquire(p)          # same (uid, rev) → reuse
    assert a is b and len(spawned) == 1 and a.initialized == 1

    c = await r.acquire(_provider(rev=1))   # credential rotated → new server
    assert c is not a and len(spawned) == 2
    await r.shutdown_all()
    assert a.closed and c.closed
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_registry.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the registry**

```python
# domains/llm_providers/services/codex_app_server_registry.py
"""One `codex app-server` process per subscription (keyed by uid+credential
revision), lazily spawned and reused across concurrent runs. The single process
owns the auth.json and its token refresh — so concurrent threads share ONE
credential with no rotation race (spike-verified). Seeds a worker-private
CODEX_HOME from the sealed secret ONCE (never per run — per OpenAI's rule)."""
from __future__ import annotations

import asyncio

from domains.llm_providers.services.codex_app_server import AppServerClient
from domains.llm_providers.services.runtime_env import apply_runtime_to_env, build_runtime
from domains.executors.agent_env import build_agent_env
from logging_config import logger


def _seed_codex_home(provider) -> dict:
    """Write the private CODEX_HOME/auth.json from the sealed secret and return
    the env (+cwd) the app-server should run with. Reuses the same seeding the
    exec path uses (build_runtime + apply_runtime_to_env)."""
    runtime = build_runtime(provider)
    env = build_agent_env(run_uid="", extra=runtime.env_vars)
    env = apply_runtime_to_env(runtime, env)
    return {"env": env, "cwd": None}


def _key(provider) -> tuple[str, int]:
    return ((provider.uid or "").strip(), int(getattr(provider, "credential_revision", 0) or 0))


class AppServerRegistry:
    def __init__(self, spawn=AppServerClient.spawn):
        self._spawn = spawn
        self._clients: dict[tuple[str, int], AppServerClient] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, key) -> asyncio.Lock:
        self._locks.setdefault(key, asyncio.Lock())
        return self._locks[key]

    async def acquire(self, provider) -> AppServerClient:
        key = _key(provider)
        async with self._lock(key):
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            seeded = _seed_codex_home(provider)
            client = await self._spawn(
                argv=["codex", "app-server", "--stdio"], env=seeded["env"], cwd=seeded["cwd"],
            )
            await client.initialize()
            self._clients[key] = client
            logger.info(f"codex app-server started for subscription {key[0]} rev {key[1]}",
                        extra={"tag": "codex"})
            return client

    async def shutdown(self, provider) -> None:
        key = _key(provider)
        client = self._clients.pop(key, None)
        if client is not None:
            await client.close()

    async def shutdown_all(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()


REGISTRY = AppServerRegistry()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_codex_app_server_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domains/llm_providers/services/codex_app_server_registry.py tests/test_codex_app_server_registry.py
git commit -m "Add per-subscription codex app-server registry (spawn+reuse)"
```

> **Note (deferred to Phase 4b):** rotation write-back on shutdown (CAS-persist the app-server's refreshed auth.json to the sealed credential, reusing `codex_credential`) and idle-timeout shutdown are cloud-fleet concerns; the process-local registry here keeps the server alive for the session. Record this in the report as a known follow-up, do not build it now (YAGNI for 4a).

---

## Task 4: `codex_cli.run_via_app_server` + MCP config object

**Files:**
- Modify: `domains/executors/mcp_bridge.py` (add `codex_mcp_config_object`)
- Modify: `domains/llm_providers/services/codex_cli.py`
- Test: `tests/test_codex_cli_app_server.py`

**Interfaces:**
- Consumes: `REGISTRY.acquire` (Task 3); `AppServerClient.start_thread`/`run_turn` (Tasks 1–2); existing `codex_mcp_overrides` (mcp_bridge).
- Produces:
  - `mcp_bridge.codex_mcp_config_object(*, run_uid: str, workspace_path: str) -> dict` — the nested-dict form of the `-c` overrides (e.g. `{"mcp_servers": {"opensweep": {"command": "npx", "args": [...]}}}`), for `thread/start.config`.
  - `codex_cli.app_server_enabled(provider) -> bool` (env `OPENSWEEP_CODEX_APP_SERVER=1` OR provider `extra_args` JSON `{"app_server": true}`).
  - `codex_cli.run_via_app_server(provider, *, instruction: str, working_dir: str, run_uid: str, model: str = "", on_delta=None, timeout_s=None) -> TurnResult`.

- [ ] **Step 1: Write the failing test for the config-object conversion**

```python
# tests/test_codex_cli_app_server.py
import json, pytest
from unittest.mock import patch
pytestmark = pytest.mark.asyncio

def test_mcp_config_object_nests_the_overrides():
    from domains.executors.mcp_bridge import codex_mcp_config_object
    with patch("domains.executors.mcp_bridge.mcp_remote_args", return_value=["-y", "mcp-remote", "URL"]):
        cfg = codex_mcp_config_object(run_uid="r1", workspace_path="")
    assert cfg["mcp_servers"]["opensweep"]["command"] == "npx"
    assert cfg["mcp_servers"]["opensweep"]["args"] == ["-y", "mcp-remote", "URL"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_codex_cli_app_server.py::test_mcp_config_object_nests_the_overrides -v`
Expected: FAIL — `codex_mcp_config_object` missing.

- [ ] **Step 3: Implement `codex_mcp_config_object`** (reuse existing override generation; parse the `key=value` TOML-ish strings into a nested dict)

```python
# mcp_bridge.py
def codex_mcp_config_object(*, run_uid: str, workspace_path: str = "") -> dict:
    """The per-run MCP servers as a nested config dict for app-server
    `thread/start.config` — same servers as the exec path's `-c` overrides
    (codex_mcp_overrides), just structured instead of flat."""
    import json as _json
    cfg: dict = {}
    for override in codex_mcp_overrides(run_uid=run_uid, workspace_path=workspace_path):
        key, _, raw = override.partition("=")
        try:
            value = _json.loads(raw)          # json.dumps output is the source; round-trips
        except _json.JSONDecodeError:
            value = raw.strip('"')
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return cfg
```

- [ ] **Step 4: Write the failing test for `run_via_app_server`** (fake client via a patched REGISTRY)

```python
async def test_run_via_app_server_starts_thread_runs_turn_and_streams(monkeypatch):
    from domains.llm_providers.services import codex_cli
    from domains.llm_providers.services.codex_app_server import TurnResult
    from types import SimpleNamespace

    calls = {}
    class _Client:
        async def start_thread(self, *, cwd, config=None, **kw):
            calls["cwd"] = cwd; calls["config"] = config; return "thr_1"
        async def run_turn(self, *, thread_id, text, on_delta=None, **kw):
            if on_delta: on_delta("hel"); on_delta("lo")
            calls["thread_id"] = thread_id; calls["text"] = text
            return TurnResult(text="hello", usage={"input_tokens": 2})
    async def fake_acquire(provider): return _Client()
    monkeypatch.setattr(codex_cli.REGISTRY, "acquire", fake_acquire)
    monkeypatch.setattr(codex_cli, "codex_mcp_config_object",
                        lambda **kw: {"mcp_servers": {"opensweep": {}}})

    seen = []
    res = await codex_cli.run_via_app_server(
        SimpleNamespace(uid="p1", kind="codex_subscription", credential_revision=0, model=""),
        instruction="do it", working_dir="/ws", run_uid="r1", on_delta=seen.append,
    )
    assert res.text == "hello" and "".join(seen) == "hello"
    assert calls["cwd"] == "/ws" and calls["text"] == "do it"
    assert calls["config"] == {"mcp_servers": {"opensweep": {}}}
```

- [ ] **Step 5: Run to verify it fails**, then **Step 6: implement** in `codex_cli.py`

Add near the top of `codex_cli.py`:
```python
import os
from domains.llm_providers.services.codex_app_server_registry import REGISTRY
from domains.executors.mcp_bridge import codex_mcp_config_object


def app_server_enabled(provider) -> bool:
    if os.environ.get("OPENSWEEP_CODEX_APP_SERVER") == "1":
        return True
    extra = getattr(provider, "extra_args", "") or ""
    try:
        import json as _json
        return bool(_json.loads(extra).get("app_server")) if extra.strip().startswith("{") else False
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
```

Run: `.venv/bin/python -m pytest tests/test_codex_cli_app_server.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add domains/executors/mcp_bridge.py domains/llm_providers/services/codex_cli.py tests/test_codex_cli_app_server.py
git commit -m "Add codex_cli.run_via_app_server + MCP config-object for thread/start"
```

---

## Task 5: Route the codex executor through the app-server when enabled

**Files:**
- Modify: `domains/executors/cli_tracking.py`
- Test: `tests/test_cli_tracking_app_server.py`

**Interfaces:**
- Consumes: `codex_cli.app_server_enabled`, `codex_cli.run_via_app_server` (Task 4); existing `append_event`, `_completed_via_mcp`, `resolve_wall_ceiling`, `DispatchResult`, `RunStatus`.
- Produces: in `_run_passes` (codex only), when `app_server_enabled(provider)`: run one turn via the app-server (streaming `on_delta` → `append_event(run_uid, "assistant_text", …)`), then build the DispatchResult from `_completed_via_mcp` (codex calls the `complete_run` MCP tool exactly as on the exec path) instead of `extract_envelope`. The exec path (continuation, envelope) is untouched when the flag is off.

- [ ] **Step 1: Write the failing test** (flag on → uses run_via_app_server, streams, no exec invoke)

```python
# tests/test_cli_tracking_app_server.py
import pytest
from types import SimpleNamespace
from domains.executors import cli_tracking
from domains.executors.base import DispatchRequest
from domains.runs.schemas import RunStatus
pytestmark = pytest.mark.asyncio


async def test_codex_dispatch_uses_app_server_when_enabled(monkeypatch):
    from domains.llm_providers.services.codex_app_server import TurnResult
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k): return provider
    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: True)

    invoked = {"exec": 0, "app_server": 0, "events": []}
    async def boom_invoke(*a, **k): invoked["exec"] += 1; raise AssertionError("exec path used")
    monkeypatch.setattr(cli_tracking, "invoke_provider", boom_invoke)

    async def fake_run(provider, *, instruction, working_dir, run_uid, model="", on_delta=None, timeout_s=None):
        invoked["app_server"] += 1
        if on_delta: on_delta("streamed ")
        return TurnResult(text="streamed answer", usage={"input_tokens": 3})
    monkeypatch.setattr(cli_tracking.codex_cli, "run_via_app_server", fake_run)
    monkeypatch.setattr(cli_tracking, "append_event", lambda uid, kind, **kw: invoked["events"].append((kind, kw)))

    async def completed(uid): return True   # codex called complete_run via MCP
    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", completed)
    # minimal request; workspace path present
    req = DispatchRequest(run_uid="r1", scheduled_agent_uid="a", repository_uid="repo1",
                          repository_local_path="/ws", intent="ask")
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())
    result = await cli_tracking.CodexAdapter().dispatch(req)

    assert invoked["app_server"] == 1 and invoked["exec"] == 0
    assert any(k == "assistant_text" for k, _ in invoked["events"])
    assert result.status in (RunStatus.COMPLETED, RunStatus.RUNNING)  # finalized by lifecycle/_completed_via_mcp

async def _async_none(): return None
```

> **Implementer:** the exact `record_input`/`recorder` monkeypatches depend on `_run_passes`' current body — read `cli_tracking._run_passes` and stub only what the app-server branch actually calls (it should NOT call the StreamRecorder/continuation machinery). Keep the three assertions above verbatim. If `dispatch`/`_run_passes` needs more neutralizing to isolate the app-server branch, that is a signal the branch should be a clean early `return await self._run_via_app_server(req, provider, started)` helper — prefer that structure.

- [ ] **Step 2: Run to verify it fails.** Expected: exec path used / attribute errors.

- [ ] **Step 3: Implement the branch** in `_run_passes` (near the top, before the exec machinery):

```python
        # App-server path (opt-in): one persistent server per subscription runs
        # this run as a thread — concurrent with other runs on the same
        # subscription, no per-run credential rotation race. Completion is the
        # same MCP `complete_run` signal as the exec path (_completed_via_mcp).
        if self.provider_kind == "codex_subscription" and codex_cli.app_server_enabled(provider):
            return await self._run_via_app_server(req, provider, started)
```

Add the helper method:

```python
    async def _run_via_app_server(self, req, provider, started):
        timeout = resolve_wall_ceiling(req, provider.kind)
        instruction = _instruction(req, timeout)
        system_prompt = _SYSTEM_PROMPT
        if code_graph_available(req.repository_local_path or ""):
            system_prompt = _SYSTEM_PROMPT + "\n" + CODE_GRAPH_PROMPT
        await record_input(req.run_uid, system_prompt=system_prompt, instruction=instruction)
        append_event(req.run_uid, "user_message", text=instruction)

        def _on_delta(text: str) -> None:
            append_event(req.run_uid, "assistant_text", text=text)

        try:
            res = await codex_cli.run_via_app_server(
                provider, instruction=f"{system_prompt}\n\n{instruction}",
                working_dir=req.repository_local_path or "", run_uid=req.run_uid,
                model=provider.model or "", on_delta=_on_delta,
                timeout_s=float(timeout) if timeout else None,
            )
        except Exception as exc:  # noqa: BLE001
            return DispatchResult(status=RunStatus.FAILED, error=f"app-server: {exc}"[:500],
                                  summary=f"{self.name.value} failed (app-server)")
        wall = time.monotonic() - started
        usage = {"wall_seconds": round(wall, 2), "provider_kind": provider.kind,
                 "transport": "app-server", **(res.usage or {})}
        if res.error:
            return DispatchResult(status=RunStatus.FAILED, error=res.error[:500], usage=usage,
                                  summary=f"{self.name.value} failed (app-server)")
        # complete_run via MCP stamps completed_at (same as exec path); lifecycle finalizes.
        completed = await _completed_via_mcp(req.run_uid)
        return DispatchResult(
            status=RunStatus.COMPLETED if completed else RunStatus.RUNNING,
            usage=usage, summary=f"{self.name.value} finished (app-server)",
        )
```

Ensure `from domains.llm_providers.services import codex_cli` is imported in `cli_tracking.py` (it already imports `codex_cli`).

- [ ] **Step 4: Run tests** — the new test + the full existing codex suite (flag OFF path unchanged):

Run: `.venv/bin/python -m pytest tests/test_cli_tracking_app_server.py tests/test_codex_run_lease.py tests/test_codex_continuation.py tests/test_codex_cli.py -v`
Expected: PASS (new app-server test; all exec-path tests unchanged because the flag defaults OFF).

- [ ] **Step 5: Commit**

```bash
git add domains/executors/cli_tracking.py tests/test_cli_tracking_app_server.py
git commit -m "Route codex runs through the app-server when OPENSWEEP_CODEX_APP_SERVER=1"
```

---

## Task 6: Live smoke verification + docs

**Files:**
- Create: `docs/superpowers/spikes/2026-07-24-app-server-smoke.md`
- Modify: `README`/provider docs note (where codex provider is documented) — mention the opt-in flag.

- [ ] **Step 1: Live smoke test (needs a real codex subscription login).**

With `OPENSWEEP_CODEX_APP_SERVER=1`, start a real Ask/Area-Map run against a small repo and confirm from the run transcript: assistant text streamed; codex reached the platform tools (MCP attached — a `create_finding`/`complete_run` call appears); the run finalized via `complete_run`; and **two concurrent runs on the same subscription both progress** (no "could not be refreshed"). Record outputs (redact tokens) in the smoke doc.

- [ ] **Step 2: If MCP does not attach**, the `thread/start.config.mcp_servers` shape needs adjustment: compare against `codex app-server generate-json-schema` for the exact config key and fix `codex_mcp_config_object` (the flat→nested conversion is the only likely culprit). Re-run the smoke test. This is the one protocol detail the spike did not exercise.

- [ ] **Step 3: Commit the smoke record + doc note.**

```bash
git add docs/superpowers/spikes/2026-07-24-app-server-smoke.md <docs-file>
git commit -m "Verify app-server run path end-to-end; document opt-in flag"
```

- [ ] **Step 4: Land Phase 4a to both repos.**

```bash
.venv/bin/python -m pytest tests/ -q            # full suite green (flag OFF default)
git push origin main
cd ../../opensweep-cloud && git fetch upstream && git merge upstream/main && \
  /Users/jeroenbrouns/Desktop/opensweep-both/opensweep/back_end/.venv/bin/python -m pytest back_end/tests/ -q -k "app_server or codex" && \
  git push origin main
```

---

## Self-Review

**Spec coverage:**
- Persistent app-server client (protocol) → Tasks 1–2. ✓
- One server per subscription, reused for concurrency, seeded once → Task 3. ✓
- `codex_cli` seam + MCP attach → Task 4. ✓
- Executor routes runs through it behind a flag; streaming + MCP completion; exec path untouched when off → Task 5. ✓
- End-to-end + the one unexercised protocol detail (MCP config shape) → Task 6 (explicit live gate). ✓
- Concurrency-on-one-subscription (the goal) → emergent from Task 3 reuse + Task 5 routing; asserted in the Task 6 smoke (two concurrent runs). ✓
- Multi-tenant fleet, rotation write-back, idle shutdown, API-key path → explicitly **Out of Scope** (Phase 4b/4c), flagged in Task 3's note. ✓

**Placeholder scan:** Tasks 1–5 carry complete code + exact commands/expected output. Task 6 is a live smoke gate (like the Phase-3 spike) — its "adjust the config shape if MCP doesn't attach" is a bounded, concrete verification, not a TODO. The Task-5 test note (read `_run_passes` and stub only what the branch calls) is guidance for a real integration point, with the assertions fixed.

**Type consistency:** `AppServerClient` (spawn/initialize/request/notify/start_thread/run_turn/close), `TurnResult(text,usage,error)`, `AppServerRegistry.acquire(provider)→client`, `codex_mcp_config_object(run_uid,workspace_path)→dict`, `run_via_app_server(provider,*,instruction,working_dir,run_uid,model,on_delta,timeout_s)→TurnResult`, `app_server_enabled(provider)→bool` are used consistently across tasks.

**Known risk:** Task 6's MCP-config shape is the sole live-unverified protocol detail; it is isolated to one function (`codex_mcp_config_object`) and gated by an explicit smoke step, so a mismatch is a one-line fix, not a redesign.
