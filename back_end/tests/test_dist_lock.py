"""Cross-process lock semantics (infrastructure/dist_lock).

Exercises the mutual-exclusion contract that holds for BOTH the Redis backend
(when up — the dev/container case) and the in-process fallback (Redis down):
one holder at a time per key, non-blocking contention returns False, release
frees the key, distinct keys are independent, and a blocking waiter gets it
after the holder releases.
"""

import asyncio

import pytest

from infrastructure.dist_lock import dist_lock

pytestmark = pytest.mark.asyncio


async def test_acquire_and_release():
    async with dist_lock("t:acquire") as got:
        assert got is True
    # Released — a fresh acquire succeeds.
    async with dist_lock("t:acquire") as got2:
        assert got2 is True


async def test_non_blocking_contention_returns_false():
    async with dist_lock("t:contend") as outer:
        assert outer is True
        async with dist_lock("t:contend", blocking=False) as inner:
            assert inner is False  # held → not acquired, no deadlock


async def test_distinct_keys_do_not_contend():
    async with dist_lock("t:a") as a, dist_lock("t:b", blocking=False) as b:
        assert a is True and b is True


async def test_blocking_waiter_gets_it_after_release():
    order: list[str] = []

    async def holder():
        async with dist_lock("t:wait", ttl_seconds=5):
            order.append("holder-acquired")
            await asyncio.sleep(0.25)
            order.append("holder-releasing")

    async def waiter():
        await asyncio.sleep(0.05)  # ensure holder goes first
        async with dist_lock("t:wait", blocking=True, blocking_timeout=5, poll_interval=0.02) as got:
            assert got is True
            order.append("waiter-acquired")

    await asyncio.gather(holder(), waiter())
    # The waiter only got the lock after the holder released it.
    assert order == ["holder-acquired", "holder-releasing", "waiter-acquired"]


async def test_blocking_timeout_returns_false():
    async with dist_lock("t:timeout"):
        async with dist_lock(
            "t:timeout", blocking=True, blocking_timeout=0.15, poll_interval=0.02
        ) as got:
            assert got is False
