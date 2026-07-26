"""Epic dispatch: turn an approved EpicProposal into implement runs.

Sibling of `campaigns.services.tick`. `plan_epic_dispatch` is the pure
decision core (which members to start now, whether the epic is finished);
`tick_epics` applies it under a redis lock (the beat fires every minute —
overlapping ticks must not double-dispatch a member) with the
refetch-before-save discipline.

Approving an epic IS the decision to work it (epic_service.approve
sets dispatch_state=pending), so nothing here starts work a human did not
already authorize.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domains.runs.services.active_runs import ACTIVE_RUN_STATUSES
from logging_config import logger

# Terminal run statuses that mean the member did NOT land. Same split as
# campaigns.tick: awaiting_input is the everyday "agent self-completed this
# turn" signal and counts as finished, not failed.
_RUN_FAILED = {"failed", "cancelled", "limit_exceeded"}

_LOCK_KEY = "opensweep:epic-tick"
_LOCK_TTL = 55  # under the 60s beat interval so a crashed tick self-heals

_DEFAULT_MAX_PARALLEL = 3

# How long an epic may sit with members it still cannot start before the tick
# gives up on it.
#
# A 409 from `trigger_implement_run` is normally transient (a write run is
# already in flight) and retrying next tick is right. But it can also be
# PERMANENT — an open PR already implements that ticket, or a human picked it
# up outside the epic. A permanently-409ing member never enters `dispatched`,
# so `complete` never becomes true and the epic would retry every minute for
# the life of the deployment, silently claiming to still be working.
#
# Measured from `dispatch_started_at`, not `updated_at`: every tick writes
# `updated_at`, so a stalled epic would keep resetting its own deadline.
_DISPATCH_DEADLINE = timedelta(hours=24)

# The only dispatch states the tick will touch. "" is deliberately absent —
# see `tick_epics`.
_TICKABLE_STATES = ("pending", "dispatching")


def epic_targets(
    shape: str,
    created_ticket_uid: str,
    member_ticket_uids: list[str] | None,
) -> list[str]:
    """The ticket uids this epic must run, in dispatch order. Pure.

    `single-pr` runs the parent ticket approval materialized — one run, one
    PR, and `trigger_implement_run` injects the children as the parent's work
    list. `parallel-runs` runs each member instead; the parent exists only to
    hold them together and is never dispatched itself, because a parent run
    plus per-member runs would race over the same tickets.
    """
    if (shape or "single-pr") == "parallel-runs":
        return [str(u) for u in (member_ticket_uids or []) if str(u)]
    return [created_ticket_uid] if created_ticket_uid else []


def dispatch_deadline_passed(
    started_at: datetime | None, now: datetime, *, deadline: timedelta = _DISPATCH_DEADLINE
) -> bool:
    """Whether an epic has been trying to start members for too long. Pure.

    A missing `started_at` means the row predates the field; treat it as NOT
    expired rather than instantly giving up on an epic that may be mid-flight
    — the deadline exists to stop infinite retries, not to fail work whose
    start time we simply do not know.
    """
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return (now - started_at) > deadline


def plan_epic_dispatch(
    targets: list[str],
    dispatched: list[dict],
    run_status_by_uid: dict[str, str],
    max_parallel: int,
    provider_headroom: int | None = None,
) -> dict:
    """Decide this tick's moves for one epic. Pure.

    A target is "dispatched" once it has an entry in `dispatched` — that
    record, not the run's status, is what stops a second run from ever being
    started for it. A dispatched run whose uid is absent from
    `run_status_by_uid` is gone (row deleted, or never persisted): it is
    counted as finished-badly rather than in flight, exactly as `plan_tick`
    treats a running part whose run vanished, because the alternative is a
    epic that never completes.

    `provider_headroom` is the target provider's remaining capacity
    (llm_providers.services.capacity). None = unknown, don't clamp. It
    already accounts for this epic's own in-flight runs, so it is a second
    independent ceiling rather than a subtraction from the first — the
    tighter of the two wins.

    `failed` is a report of members whose run ended badly, not a verdict on
    the epic: dispatching is this tick's job, and a member whose agent gave
    up is visible on the ticket and the run. Only an unexpected *dispatch*
    error fails the epic itself (see `_dispatch_one`).
    """
    started: dict[str, str] = {}  # ticket_uid → run_uid (first entry wins)
    in_flight = 0
    failed: list[str] = []
    for entry in dispatched:
        ticket_uid = str(entry.get("ticket_uid") or "")
        if not ticket_uid or ticket_uid in started:
            continue
        run_uid = str(entry.get("run_uid") or "")
        started[ticket_uid] = run_uid
        status = run_status_by_uid.get(run_uid)
        if status in ACTIVE_RUN_STATUSES:
            in_flight += 1
        elif status in _RUN_FAILED or status is None:
            failed.append(ticket_uid)

    capacity = max(int(max_parallel), 0) - in_flight
    capacity = max(capacity, 0)
    if provider_headroom is not None:
        capacity = min(capacity, max(int(provider_headroom), 0))

    dispatch: list[str] = []
    outstanding = 0
    for ticket_uid in dict.fromkeys(targets):  # stable order, deduped
        if not ticket_uid or ticket_uid in started:
            continue
        outstanding += 1
        if len(dispatch) >= capacity:
            continue
        dispatch.append(ticket_uid)

    complete = outstanding == 0 and in_flight == 0
    return {"dispatch": dispatch, "complete": complete, "failed": failed}


async def _acquire_lock() -> bool:
    """SET NX EX guard; redis unavailable ⇒ skip the tick (never crash it)."""
    from infrastructure.redis_client import get_async_redis

    try:
        return bool(
            await get_async_redis().set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"epic tick: redis lock unavailable ({type(exc).__name__}: {exc}) — skipping",
            extra={"tag": "tickets"},
        )
        return False


async def _release_lock() -> None:
    from infrastructure.redis_client import get_async_redis

    try:
        await get_async_redis().delete(_LOCK_KEY)
    except Exception:  # noqa: BLE001 — TTL expiry is the fallback
        pass


async def _provider_headroom(p) -> int | None:
    """Remaining capacity on the provider this epic's runs will use.

    Implement runs resolve their provider through the org's active provider,
    so that is what we size against. None = couldn't resolve one; don't clamp
    and let dispatch fail on its own terms rather than silently stalling the
    epic forever.
    """
    from domains.llm_providers.services.capacity import provider_headroom
    from domains.llm_providers.services.llm_provider_service import (
        get_active_provider,
        repository_org_uid,
    )

    try:
        org_uid = await repository_org_uid(p.repository_uid or "")
        provider = await get_active_provider(org_uid) if org_uid else None
        if provider is None:
            return None
        return await provider_headroom(provider)
    except Exception as exc:  # noqa: BLE001 — capacity is advisory, never fatal
        logger.warning(
            f"epic {p.uid}: provider headroom unavailable "
            f"({type(exc).__name__}: {exc}) — dispatching on the epic cap alone",
            extra={"tag": "tickets"},
        )
        return None


def _run_trigger(p):
    """MANUAL unless a schedule put this epic here.

    Mirrors campaigns.part_dispatch._campaign_trigger: the provenance that
    matters downstream is who caused the run, and for `manual`/`agent`
    proposals that is the human who approved it.
    """
    from domains.runs.schemas import RunTrigger

    return (
        RunTrigger.SCHEDULE
        if (p.origin or "") in {"rule", "schedule"}
        else RunTrigger.MANUAL
    )


async def _run_status_map(entries: list[dict]) -> dict[str, str]:
    from domains.runs.models import Run

    status_map: dict[str, str] = {}
    for uid in {str(e.get("run_uid") or "") for e in entries}:
        if not uid:
            continue
        run = await Run.nodes.get_or_none(uid=uid)
        if run is not None:
            status_map[uid] = run.status or ""
    return status_map


async def _dispatch_one(p) -> tuple[int, int]:
    """Advance one approved epic. Returns (runs started, 1 if completed)."""
    from fastapi import HTTPException

    from domains.delivery.services.implement_run_service import trigger_implement_run
    from domains.tickets.models import EpicProposal, Ticket

    targets = epic_targets(
        p.shape or "single-pr", p.created_ticket_uid or "", p.member_ticket_uids
    )
    entries = [dict(e) for e in (p.dispatched or [])]
    status_map = await _run_status_map(entries)

    # Give up on an epic that has been unable to start members for a day. The
    # 409 retry below is right for a transient conflict and wrong forever: a
    # member whose ticket already has an open PR will 409 on every tick until
    # the heat death of the deployment, and the epic would keep reporting
    # `dispatching` as though work were still coming.
    started_uids = {str(e.get("ticket_uid") or "") for e in entries}
    outstanding = [t for t in dict.fromkeys(targets) if t not in started_uids]
    if outstanding and dispatch_deadline_passed(
        p.dispatch_started_at, datetime.now(UTC)
    ):
        await _fail_epic(
            p,
            "gave up after 24h — could not start: " + ", ".join(outstanding[:10]),
        )
        return 0, 0
    max_parallel = int(p.max_parallel or _DEFAULT_MAX_PARALLEL)
    decision = plan_epic_dispatch(
        targets,
        entries,
        status_map,
        max_parallel,
        await _provider_headroom(p),
    )

    async def _persist(state: str) -> bool:
        # Refetch-before-save: a reviewer rejecting (or an operator cancelling)
        # the epic between load and save must not be clobbered by this tick.
        fresh = await EpicProposal.nodes.get_or_none(uid=p.uid)
        if fresh is None:
            return False
        if (fresh.status or "") != "approved" or (
            fresh.dispatch_state or ""
        ) not in _TICKABLE_STATES:
            return False
        fresh.dispatched = entries
        fresh.dispatch_state = state
        fresh.updated_at = datetime.now(UTC)
        await fresh.save()
        return True

    started = 0
    for ticket_uid in decision["dispatch"]:
        ticket = await Ticket.nodes.get_or_none(uid=ticket_uid)
        if ticket is None:
            # Deleted after approval. Record it as handled with no run:
            # leaving it outstanding would keep the epic in `dispatching`
            # forever, re-looking-it-up every minute.
            entries.append(
                {"ticket_uid": ticket_uid, "run_uid": "", "error": "ticket not found"}
            )
            if not await _persist("dispatching"):
                return started, 0
            continue
        try:
            run = await trigger_implement_run(
                ticket,
                triggered_by=p.reviewed_by or "epic-dispatch",
                trigger=_run_trigger(p),
            )
        except HTTPException as exc:
            # 409 is NORMAL here, not an epic failure: a write run is already
            # in flight for this ticket, an open PR already implements it, or
            # the ticket has not passed Gate 1 yet. Someone/something else is
            # doing this member's work — leave it undispatched and look again
            # next tick rather than recording a run we did not start.
            if exc.status_code != 409:
                raise
            logger.info(
                f"epic {p.uid}: ticket {ticket_uid} not dispatchable this tick "
                f"({exc.detail}) — retrying next tick",
                extra={"tag": "tickets"},
            )
            continue
        entries.append({"ticket_uid": ticket_uid, "run_uid": run.uid})
        status_map[run.uid] = run.status or "queued"
        started += 1
        # Persist after every dispatch: a crash before save would leave the
        # member undispatched while its run exists, and the next tick would
        # start a duplicate and orphan this one.
        if not await _persist("dispatching"):
            return started, 0

    # Recompute completion AFTER the dispatch results — a vanished ticket or a
    # run that finished mid-loop may have just closed out the epic. Headroom
    # is irrelevant to the completion question, so it is not consulted here.
    final = plan_epic_dispatch(targets, entries, status_map, max_parallel, None)
    state = "done" if final["complete"] else "dispatching"
    if not await _persist(state):
        return started, 0
    return started, 1 if state == "done" else 0


async def _fail_epic(p, reason: str) -> None:
    """Mark an epic failed, recording why on the row and in the audit log.

    An epic stuck in `failed` whose only explanation is a celery traceback is
    not debuggable, so the reason lands on `last_error` where the DTO and the
    review UI can read it. It does NOT go into `dispatched`: that list is the
    epic's runs, and a sentinel row with no ticket_uid corrupts every
    consumer that iterates it.
    """
    from domains.tickets.models import EpicProposal
    from infrastructure.audit import write_audit

    try:
        fresh = await EpicProposal.nodes.get_or_none(uid=p.uid)
        if fresh is not None and (fresh.dispatch_state or "") in _TICKABLE_STATES:
            fresh.last_error = reason
            fresh.dispatch_state = "failed"
            fresh.updated_at = datetime.now(UTC)
            await fresh.save()
        await write_audit(
            kind="epic.dispatch_failed",
            subject_uid=p.uid,
            subject_type="EpicProposal",
            actor_uid="system",
            payload={"error": reason},
        )
    except Exception:  # noqa: BLE001 — never let the bookkeeping stall the tick
        pass


async def tick_epics() -> dict:
    """Dispatch implement runs for approved epics; retire finished ones."""
    from domains.tickets.models import EpicProposal

    if not await _acquire_lock():
        return {"skipped": True}

    dispatched = 0
    completed = 0
    errors = 0
    try:
        # `dispatch_state=""` is EXCLUDED on purpose. An approved row with an
        # empty state was approved before epic dispatch existed; treating it
        # as pending would fan write runs out across the entire history of
        # approved epics the first time this beat ran.
        rows = await EpicProposal.nodes.filter(
            status="approved", dispatch_state__in=list(_TICKABLE_STATES)
        )
        for p in rows:
            try:
                d, c = await _dispatch_one(p)
                dispatched += d
                completed += c
            except Exception as exc:  # noqa: BLE001 — one epic never stalls the rest
                errors += 1
                reason = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"epic {p.uid}: dispatch failed: {reason}",
                    extra={"tag": "tickets"},
                )
                await _fail_epic(p, reason)
    finally:
        # Release eagerly — a slow tick (many dispatches) must not make the
        # next beat wait out the TTL.
        await _release_lock()

    return {"dispatched": dispatched, "completed": completed, "errors": errors}
