"""Campaign tick: advance every running campaign one step.

`plan_tick` is the pure decision core (what to mark, what to dispatch,
whether the campaign is complete); `tick_campaigns` applies it under a
redis lock (beat fires every minute — overlapping ticks must not
double-dispatch parts) with the refetch-before-save discipline.
"""

from __future__ import annotations

from datetime import UTC, datetime

from logging_config import logger

# Child run statuses that end a part. awaiting_input IS the everyday
# "the agent self-completed this turn" signal (see RunStatus).
_RUN_DONE = {"awaiting_input", "ended", "completed"}
_RUN_FAILED = {"failed", "cancelled", "limit_exceeded"}

_LOCK_KEY = "opensweep:campaign-tick"
_LOCK_TTL = 55  # under the 60s beat interval so a crashed tick self-heals


def plan_tick(
    parts: list[dict], run_status_by_uid: dict[str, str], max_parallel: int
) -> dict:
    """Decide this tick's moves. Pure.

    Done/failed parts NEVER revert — only pending/running parts are
    considered. A running part whose run vanished from the map is failed
    (the row was deleted or never persisted); anything non-terminal
    (running, queued, paused_quota) stays in flight. Global parts become
    dispatchable only once every area part is terminal, so their
    escalation digests see the full campaign's findings.
    """
    mark_done: list[int] = []
    mark_failed: list[int] = []
    in_flight = 0
    for part in parts:
        if part.get("state") != "running":
            continue
        idx = int(part["idx"])
        run_uid = part.get("run_uid") or ""
        status = run_status_by_uid.get(run_uid)
        if status in _RUN_DONE:
            mark_done.append(idx)
        elif status in _RUN_FAILED or status is None:
            mark_failed.append(idx)
        else:
            in_flight += 1

    def _state_after(part: dict) -> str:
        idx = int(part["idx"])
        if idx in mark_done:
            return "done"
        if idx in mark_failed:
            return "failed"
        return str(part.get("state") or "pending")

    areas_terminal = all(
        _state_after(p) in {"done", "failed"}
        for p in parts
        if (p.get("kind") or "area") != "global"
    )

    capacity = max(int(max_parallel) - in_flight, 0)
    dispatch: list[int] = []
    pending_left = 0
    for part in sorted(parts, key=lambda p: int(p["idx"])):
        if part.get("state") != "pending":
            continue
        pending_left += 1
        if len(dispatch) >= capacity:
            continue
        if (part.get("kind") or "area") == "global" and not areas_terminal:
            continue
        dispatch.append(int(part["idx"]))

    complete = pending_left == 0 and in_flight == 0
    return {
        "mark_done": mark_done,
        "mark_failed": mark_failed,
        "dispatch": dispatch,
        "complete": complete,
    }


async def _acquire_lock() -> bool:
    """SET NX EX guard; redis unavailable ⇒ skip the tick (never crash it)."""
    from infrastructure.redis_client import get_async_redis

    try:
        return bool(
            await get_async_redis().set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"campaign tick: redis lock unavailable ({type(exc).__name__}: {exc}) — skipping",
            extra={"tag": "campaigns"},
        )
        return False


async def _release_lock() -> None:
    from infrastructure.redis_client import get_async_redis

    try:
        await get_async_redis().delete(_LOCK_KEY)
    except Exception:  # noqa: BLE001 — TTL expiry is the fallback
        pass


async def _finalize_guarded(campaign) -> bool:
    """Finalize, recording a visible event on failure.

    A campaign stuck in `finalizing` has no legal manual exit, so a
    deterministic finalize crash must at least leave a trail on the row —
    otherwise the only signal is a celery traceback.
    """
    from domains.campaigns.models import Campaign
    from domains.campaigns.services.finalize import finalize_campaign

    try:
        await finalize_campaign(campaign)
        return True
    except Exception as exc:  # noqa: BLE001 — one campaign never stalls the tick
        logger.warning(
            f"campaign {campaign.uid}: finalize failed: {type(exc).__name__}: {exc}",
            extra={"tag": "campaigns"},
        )
        try:
            fresh = await Campaign.nodes.get_or_none(uid=campaign.uid)
            if fresh is not None:
                fresh.events = [
                    *(fresh.events or []),
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "type": "finalize_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                ]
                await fresh.save()
        except Exception:  # noqa: BLE001
            pass
        return False


async def _tick_one(c) -> tuple[int, int]:
    """Advance one running campaign. Returns (dispatched, finalized)."""
    from domains.campaigns.models import Campaign, is_legal_status_transition
    from domains.campaigns.services.part_dispatch import dispatch_part
    from domains.runs.models import Run

    dispatched = 0
    parts = [dict(p) for p in (c.parts or [])]
    status_map: dict[str, str] = {}
    for uid in {p.get("run_uid") or "" for p in parts if p.get("run_uid")}:
        run = await Run.nodes.get_or_none(uid=uid)
        if run is not None:
            status_map[uid] = run.status or ""
    decision = plan_tick(parts, status_map, int(c.max_parallel or 2))

    now = datetime.now(UTC)
    events: list[dict] = []
    by_idx = {int(p["idx"]): p for p in parts}
    for idx in decision["mark_done"]:
        by_idx[idx]["state"] = "done"
        events.append({"ts": now.isoformat(), "type": "part_done", "part": idx})
    for idx in decision["mark_failed"]:
        by_idx[idx]["state"] = "failed"
        events.append({"ts": now.isoformat(), "type": "part_failed", "part": idx})

    async def _persist() -> bool:
        # Refetch-before-save (thread_service.record_event): a concurrent
        # cancel between load and save must not be clobbered.
        nonlocal events
        fresh = await Campaign.nodes.get_or_none(uid=c.uid) or c
        if fresh.status != "running":
            return False
        fresh.parts = parts
        fresh.events = [*(fresh.events or []), *events]
        fresh.updated_at = datetime.now(UTC)
        await fresh.save()
        events = []
        return True

    for idx in decision["dispatch"]:
        part = by_idx[idx]
        try:
            run_uid = await dispatch_part(c, part)
        except Exception as exc:  # noqa: BLE001 — one bad part never stops the tick
            part["state"] = "failed"
            events.append(
                {
                    "ts": now.isoformat(),
                    "type": "part_dispatch_failed",
                    "part": idx,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.warning(
                f"campaign {c.uid}: part {idx} dispatch failed: {exc}",
                extra={"tag": "campaigns"},
            )
            continue
        part["state"] = "running"
        part["run_uid"] = run_uid
        dispatched += 1
        events.append(
            {
                "ts": now.isoformat(),
                "type": "part_dispatched",
                "part": idx,
                "run_uid": run_uid,
            }
        )
        # Persist after every dispatch: a crash before save would otherwise
        # leave the part pending while its run exists — the next tick would
        # dispatch a duplicate and orphan this one.
        if not await _persist():
            return dispatched, 0

    # Recompute completion AFTER dispatch results: a failed dispatch may
    # have just finished the last outstanding part.
    complete = all(p.get("state") in {"done", "failed"} for p in parts)

    fresh = await Campaign.nodes.get_or_none(uid=c.uid) or c
    if fresh.status != "running":
        return dispatched, 0
    fresh.parts = parts
    fresh.events = [*(fresh.events or []), *events]
    fresh.updated_at = datetime.now(UTC)
    if complete and is_legal_status_transition(fresh.status, "finalizing"):
        fresh.status = "finalizing"
    await fresh.save()
    if fresh.status == "finalizing":
        return dispatched, 1 if await _finalize_guarded(fresh) else 0
    return dispatched, 0


async def tick_campaigns() -> dict:
    """Advance running campaigns; re-run finalize for crashed finalizers."""
    from domains.campaigns.models import Campaign

    if not await _acquire_lock():
        return {"skipped": True}

    ticked = 0
    dispatched = 0
    finalized = 0
    errors = 0
    try:
        from domains.campaigns.services import batch

        for c in await Campaign.nodes.filter(status="running"):
            ticked += 1
            # Batch parents own no parts — they roll their children up instead
            # of dispatching. aggregate_batch is a no-op until all children
            # are terminal, then it finalizes the parent.
            if str(getattr(c, "kind", "") or "") == "batch":
                try:
                    if await batch.aggregate_batch(c):
                        finalized += 1
                except Exception as exc:  # noqa: BLE001 — one batch never stalls the rest
                    errors += 1
                    logger.warning(
                        f"campaign {c.uid}: batch aggregate failed: {type(exc).__name__}: {exc}",
                        extra={"tag": "campaigns"},
                    )
                continue
            try:
                d, f = await _tick_one(c)
                dispatched += d
                finalized += f
            except Exception as exc:  # noqa: BLE001 — one campaign never stalls the rest
                errors += 1
                logger.warning(
                    f"campaign {c.uid}: tick failed: {type(exc).__name__}: {exc}",
                    extra={"tag": "campaigns"},
                )

        # Crash recovery: a tick that died mid-finalize left the row in
        # finalizing — finalize is idempotent, run it again.
        for c in await Campaign.nodes.filter(status="finalizing"):
            if await _finalize_guarded(c):
                finalized += 1
            else:
                errors += 1
    finally:
        # Release eagerly — a slow tick (many finalizations) must not make
        # the next beat wait out the TTL.
        await _release_lock()

    return {
        "ticked": ticked,
        "dispatched": dispatched,
        "finalized": finalized,
        "errors": errors,
    }
