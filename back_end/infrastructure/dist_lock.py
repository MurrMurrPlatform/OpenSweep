"""Cross-process advisory lock (Redis) with an in-process fallback.

Dispatch decisions like "is there already an active write run for this PR?"
are read-then-act: read the state, then create a run. The backend AND the
Celery worker both do this, so a per-process `asyncio.Lock` (what the code used)
serialized only WITHIN one process — the two could each pass the "no active run"
check and double-dispatch. This lock is held across the read+write so exactly
one process proceeds.

Backend: `SET key token NX PX ttl` for cross-process mutual exclusion; release
is token-guarded (compare-and-del via Lua) so a slow holder whose lock already
expired cannot delete a lock a NEW owner has since taken. A TTL bounds a crashed
holder — the lock is never held forever.

Fallback: when Redis is unreachable (a dev box without it, or an outage) we use
a per-key in-process `asyncio.Lock`, so single-process behavior is unchanged and
a Redis blip never wedges dispatch. (In a genuinely multi-process deployment
with Redis down, mutual exclusion degrades to per-process — acceptable: Redis is
required infrastructure there, and this only affects the rare outage window.)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

from logging_config import logger

_NS = "opensweep:lock:"

# Release iff we still own it: delete only when the stored token is ours. Guards
# against deleting a lock a newer holder took after ours expired.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)

_local_locks: dict[str, asyncio.Lock] = {}


def _local(key: str) -> asyncio.Lock:
    lk = _local_locks.get(key)
    if lk is None:
        lk = _local_locks[key] = asyncio.Lock()
    return lk


async def _acquire_local(lk: asyncio.Lock, *, blocking: bool, timeout: float) -> bool:
    if not blocking:
        # No await between the check and acquire → atomic within one loop.
        if lk.locked():
            return False
        await lk.acquire()
        return True
    try:
        await asyncio.wait_for(lk.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


@contextlib.asynccontextmanager
async def dist_lock(
    key: str,
    *,
    ttl_seconds: float = 60.0,
    blocking: bool = True,
    blocking_timeout: float = 30.0,
    poll_interval: float = 0.1,
):
    """Acquire the cross-process lock named `key`.

    Yields True if the lock was acquired (release is automatic on exit), or
    False if `blocking` is False (or the wait timed out) and it was held by
    someone else — the caller decides what to do (skip, 409, retry).

    Blocking waits by polling `SET NX` up to `blocking_timeout`; the TTL bounds a
    crashed holder so a wait can never hang past `ttl_seconds` on a dead owner.
    """
    full = _NS + key
    token = uuid.uuid4().hex
    px = int(ttl_seconds * 1000)

    # Try Redis; fall back to the in-process lock if Redis is unusable.
    redis = None
    try:
        from infrastructure.redis_client import get_async_redis

        redis = get_async_redis()
    except Exception:
        redis = None

    if redis is not None:
        acquired = False
        deadline = time.monotonic() + blocking_timeout
        while True:
            try:
                acquired = bool(await redis.set(full, token, nx=True, px=px))
            except Exception as exc:
                # Redis went away mid-acquire — degrade to the in-process lock
                # for this call rather than fail dispatch.
                logger.warning(
                    f"dist_lock {key}: redis unavailable ({exc}); using in-process lock",
                    extra={"tag": "lock"},
                )
                redis = None
                break
            if acquired or not blocking or time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval)

        if redis is not None:
            try:
                yield acquired
            finally:
                if acquired:
                    with contextlib.suppress(Exception):
                        await redis.eval(_RELEASE_LUA, 1, full, token)
            return

    # In-process fallback (no Redis, or it failed above).
    lk = _local(key)
    got = await _acquire_local(lk, blocking=blocking, timeout=blocking_timeout)
    try:
        yield got
    finally:
        if got:
            lk.release()
