"""One `codex app-server` process per subscription, and — critically — **one
credential holder platform-wide**.

Phase 4a spawned an app-server per worker process. That is unsafe under the
production Celery worker (`--pool=prefork --concurrency=10`): ten processes each
spawn their own app-server, all reading one sealed credential, and each performs
its own OAuth refresh of a *single-use rotating* refresh token — the exact
"access token could not be refreshed" race the whole effort fixed.

Phase 4b closes that by making the **app-server itself the lease holder**. A
session enters `codex_credential.codex_credential_txn` when it spawns and holds
it for the server's whole lifetime, so:

  * exactly one process anywhere touches that auth.json at a time (OpenAI's
    "one auth.json per runner, no concurrent sharing" rule), enforced by the
    same durable Neo4j lease the exec path already uses, with the same TTL
    renewal and the same compare-and-swap write-back of a rotated token;
  * within that one holder, *many* runs share the server concurrently — each
    gets its own codex thread. That is the win over the exec path, which had to
    serialize whole runs to stay safe.

A run that lands on a process which cannot get the lease raises the same
`HTTPException` the exec path raises, and the caller pauses it for retry.

Sessions are refcounted and shut down after an idle period, which releases the
lease (and writes back any rotation) rather than pinning the subscription to
whichever process touched it first.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from domains.llm_providers.services import codex_credential
from domains.llm_providers.services.codex_app_server import AppServerClient
from domains.llm_providers.services.runtime_env import apply_runtime_to_env, build_runtime
from domains.executors.agent_env import build_agent_env
from logging_config import logger


def _idle_shutdown_seconds() -> float:
    """How long a session with no in-flight turns keeps the server (and the
    lease) before releasing. Long enough that a burst of runs reuses one warm
    server; short enough that an idle subscription frees up for other processes."""
    try:
        return max(0.0, float(os.environ.get("OPENSWEEP_CODEX_APP_SERVER_IDLE_SECONDS", "120")))
    except ValueError:
        return 120.0


def long_lived_loop() -> bool:
    """True only in a process whose event loop outlives a single run.

    The FastAPI backend runs one loop for the process lifetime, so a session can
    span many runs there — that is where the concurrency win comes from, and it
    covers the user-facing surfaces (Ask, Area Map, actions), which dispatch
    in-process via `lifecycle._launch_dispatch`.

    A Celery worker is the opposite: every run is its own task calling
    `asyncio.run` (`tasks/dispatch_runs.dispatch_run`), so the loop dies with the
    run. A session cached in this module-level registry would outlive its loop,
    and `asyncio.run`'s `shutdown_asyncgens()` would force-finalize the parked
    `codex_credential_txn` generator — releasing the lease while the codex
    process is still alive and still able to refresh the rotating token. Another
    prefork child would then take the free lease and spawn a SECOND app-server on
    the same auth.json: exactly the rotation race this design exists to prevent.
    So worker runs stay on the `exec` path, which takes a per-run lease and is
    safe by construction.
    """
    from infrastructure.process_role import WORKER, get_role

    return get_role() != WORKER


def _seed_env(provider) -> dict:
    """The env the app-server runs with. `codex_credential_txn` has already
    written the current credential into the revision-scoped CODEX_HOME under the
    lease; this just points the process at it (same seeding the exec path uses)."""
    runtime = build_runtime(provider)
    env = build_agent_env(run_uid="", extra=runtime.env_vars)
    return apply_runtime_to_env(runtime, env)


@dataclass
class AppServerSession:
    """A live app-server plus the credential lease it holds."""

    uid: str
    revision: int
    client: AppServerClient
    stack: AsyncExitStack
    refs: int = 0
    idle_task: asyncio.Task | None = field(default=None, repr=False)
    # The loop this session was built on. A session CANNOT outlive it: the lease
    # is parked as a suspended async generator in `stack`, and `asyncio.run`
    # teardown calls `loop.shutdown_asyncgens()`, which force-finalizes that
    # generator — releasing the lease while the codex process is still alive and
    # still refreshing the token. See `_long_lived_loop`.
    loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)


class AppServerRegistry:
    def __init__(self, spawn=AppServerClient.spawn):
        self._spawn = spawn
        self._sessions: dict[str, AppServerSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closing = False

    def _lock(self, uid: str) -> asyncio.Lock:
        self._locks.setdefault(uid, asyncio.Lock())
        return self._locks[uid]

    @staticmethod
    def _uid(provider) -> str:
        uid = (getattr(provider, "uid", "") or "").strip()
        if not uid:
            raise ValueError("codex app-server registry: provider.uid is required")
        return uid

    async def acquire(self, provider) -> AppServerSession:
        """Return a live session for this subscription, spawning (and taking the
        credential lease) if needed. Raises `HTTPException` when another process
        holds the lease past the wait budget — same contract as the exec path."""
        uid = self._uid(provider)
        async with self._lock(uid):
            session = self._sessions.get(uid)

            if session is not None and session.loop is not asyncio.get_running_loop():
                # Its loop is gone (or this is a different one), so its lease was
                # already force-released and its reader task cancelled — the
                # session is unusable. Drop it WITHOUT touching the dead loop.
                logger.warning(
                    f"codex app-server for {uid} was built on a different event loop "
                    f"— discarding (app-server path expects a long-lived loop)",
                    extra={"tag": "codex"},
                )
                self._sessions.pop(uid, None)
                session = None

            if session is not None and not session.client.alive:
                logger.info(f"codex app-server for {uid} is dead — recycling",
                            extra={"tag": "codex"})
                await self._close(session)
                session = None

            if session is not None and session.refs == 0:
                # The user re-pasting the credential bumps the revision; a session
                # still running on the old one would seed stale tokens into every
                # new run, so retire it (its write-back CAS drops harmlessly).
                # Only while idle — tearing the server down under in-flight turns
                # would kill other runs, and the old credential still works.
                try:
                    _, current_revision = await codex_credential._read_credential(uid)
                except Exception as exc:  # noqa: BLE001
                    # A transient DB blip must not fail the run — keep the warm
                    # session; a genuinely stale credential surfaces as a codex
                    # auth error, and the next acquire re-checks.
                    logger.warning(f"codex subscription {uid}: revision probe failed: {exc}",
                                   extra={"tag": "codex"})
                    current_revision = session.revision
                if current_revision != session.revision:
                    logger.info(
                        f"codex subscription {uid}: credential moved rev "
                        f"{session.revision} → {current_revision} — recycling app-server",
                        extra={"tag": "codex"},
                    )
                    await self._close(session)
                    session = None

            if self._closing:
                raise RuntimeError("codex app-server registry is shutting down")

            if session is None:
                session = await self._start(provider, uid)
                if self._closing:
                    # A shutdown started while we were spawning; nothing would
                    # ever close this session, so unwind it here.
                    await self._close(session)
                    raise RuntimeError("codex app-server registry is shutting down")
                self._sessions[uid] = session

            session.refs += 1
            self._cancel_idle(session)
            return session

    async def _start(self, provider, uid: str) -> AppServerSession:
        stack = AsyncExitStack()
        # Held OPEN for the server's lifetime: acquires the exclusive lease,
        # re-reads the credential under it, seeds CODEX_HOME, and renews the
        # lease in the background. Closing it writes back any rotation (CAS) and
        # releases. Raises HTTPException if the lease is held elsewhere.
        await stack.enter_async_context(codex_credential.codex_credential_txn(provider))
        try:
            client = await self._spawn(
                argv=["codex", "app-server", "--stdio"], env=_seed_env(provider), cwd=None,
            )
            await client.initialize()
        except BaseException:
            # Never strand the lease if the server fails to come up.
            await stack.aclose()
            raise
        revision = int(getattr(provider, "credential_revision", 0) or 0)
        logger.info(f"codex app-server started for subscription {uid} rev {revision}",
                    extra={"tag": "codex"})
        return AppServerSession(uid=uid, revision=revision, client=client, stack=stack,
                                loop=asyncio.get_running_loop())

    async def release(self, session: AppServerSession) -> None:
        """Drop one in-flight turn. The last release starts the idle countdown."""
        async with self._lock(session.uid):
            session.refs -= 1
            if session.refs > 0 or self._sessions.get(session.uid) is not session:
                return
            session.refs = 0
            idle = _idle_shutdown_seconds()
            if idle <= 0:
                await self._close(session)
                return
            session.idle_task = asyncio.create_task(self._close_when_idle(session, idle))

    async def _close_when_idle(self, session: AppServerSession, idle: float) -> None:
        try:
            await asyncio.sleep(idle)
        except asyncio.CancelledError:
            return
        async with self._lock(session.uid):
            # A run may have grabbed it while we slept.
            if session.refs > 0 or self._sessions.get(session.uid) is not session:
                return
            # Detach first: `_close` cancels `session.idle_task`, which IS this
            # task. Cancelling ourselves here would raise CancelledError at the
            # first await inside `_close` and skip the lease teardown entirely,
            # stranding the subscription until its TTL expires.
            session.idle_task = None
            logger.info(f"codex app-server for {session.uid} idle {idle:.0f}s — releasing "
                        f"subscription", extra={"tag": "codex"})
            await self._close(session)

    @staticmethod
    def _cancel_idle(session: AppServerSession) -> None:
        if session.idle_task is not None:
            session.idle_task.cancel()
            session.idle_task = None

    async def _close(self, session: AppServerSession) -> None:
        """Stop the server, then write back + release the lease. Caller holds the
        per-uid lock. Never raises — teardown must not fail a run."""
        self._cancel_idle(session)
        if self._sessions.get(session.uid) is session:
            self._sessions.pop(session.uid, None)
        try:
            # BaseException, not Exception: a CancelledError here must not skip
            # the teardown below, or the subscription stays locked until TTL.
            await session.client.close()
        except BaseException as exc:  # noqa: BLE001
            logger.warning(f"codex app-server {session.uid}: close failed: {exc}",
                           extra={"tag": "codex"})
        try:
            # Runs the txn's exit: CAS write-back of a rotated auth.json, then
            # release. Must happen AFTER the server is gone so the file is final.
            await session.stack.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"codex subscription {session.uid}: lease teardown failed: {exc}",
                           extra={"tag": "codex"})

    async def shutdown(self, provider) -> None:
        uid = self._uid(provider)
        async with self._lock(uid):
            session = self._sessions.get(uid)
            if session is not None:
                await self._close(session)

    async def shutdown_all(self) -> None:
        """Release every subscription this process holds. Called on worker/app
        shutdown so a restart doesn't strand leases until their TTL expires.

        Sets `_closing` first so a session whose `_start` is still in flight is
        refused rather than registered behind our back — it would keep the lease
        with nothing left to release it. `_locks` is deliberately NOT cleared:
        discarding a Lock another coroutine holds or waits on would mint a fresh
        one and let two coroutines into the same critical section. The dict is
        bounded by the number of subscriptions seen, which is small.
        """
        self._closing = True
        try:
            while self._sessions:
                uid = next(iter(self._sessions))
                async with self._lock(uid):
                    session = self._sessions.get(uid)
                    if session is not None:
                        await self._close(session)
                    else:
                        self._sessions.pop(uid, None)
        finally:
            self._closing = False


REGISTRY = AppServerRegistry()


def shutdown_all_blocking() -> None:
    """Sync entry point for process-exit hooks (Celery worker shutdown), which
    run without the worker's event loop.

    Goes through `run_async_task` because the teardown does Neo4j writes (the
    rotation write-back and the lease release) and the async driver is still
    bound to the dead loop. Killing the codex process itself is a signal, so it
    lands regardless; a lease we fail to release self-expires on its TTL.
    """
    if not REGISTRY._sessions:
        return
    with contextlib.suppress(Exception):
        from infrastructure.celery_async import run_async_task

        run_async_task(REGISTRY.shutdown_all)
