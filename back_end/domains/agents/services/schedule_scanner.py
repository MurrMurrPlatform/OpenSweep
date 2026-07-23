"""Scan ScheduledAgents for due cron triggers and dispatch Runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from croniter import croniter
from fastapi import HTTPException

from domains.agents.models import Agent, ScheduledAgent
from domains.agents.schemas import parse_trigger
from domains.agents.services.registry import agent_key
from domains.runs.schemas import RunTrigger
from logging_config import logger


@dataclass
class ScanResult:
    scanned: int = 0
    dispatched: int = 0
    skipped_invalid: int = 0
    errors: list[str] = field(default_factory=list)


def is_due(expression: str, *, last: datetime | None, now: datetime) -> bool:
    """A cron is due if its previous fire time falls in (last, now]."""
    if not croniter.is_valid(expression):
        raise ValueError(f"invalid crontab: {expression!r}")
    prev_fire = croniter(expression, now).get_prev(datetime)
    if prev_fire.tzinfo is None:
        prev_fire = prev_fire.replace(tzinfo=timezone.utc)
    if last is None:
        return prev_fire <= now
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return prev_fire > last


def should_auto_audit(key: str, autonomy: str) -> bool:
    """audit-stale bindings fan out via run_auto_audit instead of a single
    dispatch; `disabled` is the kill-safety even with a cron set."""
    return (key or "") == "audit-stale" and (autonomy or "") != "disabled"


async def scan_and_dispatch(*, now: datetime | None = None) -> ScanResult:
    """Iterate ScheduledAgents with cron triggers and dispatch any that are due."""
    # Imported lazily so the pure is_due helper above stays importable without
    # pulling the full executor/config stack.
    from domains.agents.services.dispatch import trigger_scheduled_agent
    from domains.runs.services.lifecycle import LifecycleError

    moment = now or datetime.now(timezone.utc)
    result = ScanResult()
    for sa in await ScheduledAgent.nodes.all():
        if not sa.enabled:
            continue
        try:
            kind, payload = parse_trigger(sa.trigger or "")
        except ValueError:
            result.skipped_invalid += 1
            continue
        if kind != "cron":
            continue
        result.scanned += 1
        try:
            due = is_due(payload, last=sa.last_scheduled_at, now=moment)
        except ValueError as exc:
            result.skipped_invalid += 1
            result.errors.append(f"{sa.uid}: {exc}")
            continue
        if not due:
            continue
        agent = await Agent.nodes.get_or_none(uid=sa.agent_uid)
        key = agent_key(agent.source_url or "") if agent else ""
        if key == "audit-stale":
            # Staleness-driven fan-out (§F): one scoped audit per selected
            # page instead of a single run off this binding.
            if not should_auto_audit(key, sa.autonomy or ""):
                sa.last_scheduled_at = moment
                await sa.save()
                continue
            from domains.runs.services.sweep import run_auto_audit

            try:
                audit = await run_auto_audit(
                    repository_uid=sa.repository_uid,
                    limit=int((sa.target or {}).get("limit") or 3),
                    triggered_by=f"cron:{payload}",
                )
            except Exception as exc:  # noqa: BLE001 — one bad repo never stops the scan
                result.errors.append(f"{sa.uid}: {type(exc).__name__}: {exc}")
                # Stamp the tick even on failure (like every sibling branch):
                # without it a failing repo re-fires this binding every beat,
                # thrashing the audit log until the fault clears.
                sa.last_scheduled_at = moment
                await sa.save()
                continue
            sa.last_scheduled_at = moment
            await sa.save()
            result.dispatched += len(audit.runs_dispatched)
            logger.info(
                f"schedule auto-audit sa={sa.uid} expr={payload} "
                f"dispatched={len(audit.runs_dispatched)}",
                extra={"tag": "schedule"},
            )
            continue
        if key == "run-campaign":
            # Campaign anchor: a due tick plans + launches a campaign from
            # the binding's target instead of dispatching a single run. A
            # scheduled campaign is pre-approved — launch immediately;
            # `disabled` stays the kill-safety even with a cron set.
            if (sa.autonomy or "") == "disabled":
                sa.last_scheduled_at = moment
                await sa.save()
                continue
            from domains.campaigns.schemas import CreateCampaignRequest
            from domains.campaigns.services import campaign_service

            tgt = dict(sa.target or {})
            campaign = None
            try:
                campaign = await campaign_service.create(
                    sa.repository_uid,
                    CreateCampaignRequest(
                        template=str(tgt.get("template") or "rotation"),
                        kind=str(tgt.get("kind") or ""),
                        coverage_keys=[str(x) for x in (tgt.get("coverage_keys") or [])],
                        selection=str(tgt.get("selection") or ""),
                        k=int(tgt.get("k") or 3),
                        lens_keys=[str(k) for k in (tgt.get("lens_keys") or [])],
                        effort=str(tgt.get("effort") or ""),
                        area_prefix=str(tgt.get("area_prefix") or ""),
                        max_parallel=int(tgt.get("max_parallel") or 2),
                    ),
                    created_by=f"scheduled-agent:{sa.uid}",
                    trigger_provenance=sa.trigger or "",
                )
                await campaign_service.launch(campaign.uid)
            except Exception as exc:  # noqa: BLE001 — one bad repo never stops the scan
                result.errors.append(f"{sa.uid}: {type(exc).__name__}: {exc}")
                if campaign is not None:
                    # Created but not launched: cancel it, or every retry
                    # tick strands another campaign in `planning`.
                    try:
                        await campaign_service.cancel(
                            campaign.uid, reason="scheduled launch failed"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                if isinstance(exc, HTTPException) and 400 <= exc.status_code < 500:
                    # Deterministic misconfig (bad template/lenses in target):
                    # retrying every minute cannot help — stamp the tick so
                    # the binding waits for its next cron slot.
                    sa.last_scheduled_at = moment
                    await sa.save()
                continue
            sa.last_scheduled_at = moment
            await sa.save()
            result.dispatched += 1
            logger.info(
                f"schedule campaign sa={sa.uid} expr={payload} campaign={campaign.uid}",
                extra={"tag": "schedule"},
            )
            continue
        if key == "map-areas":
            # Area-map refresh: a due tick dispatches one map-areas run that
            # re-proposes the repository's audit partition; `disabled` stays
            # the kill-safety even with a cron set.
            if (sa.autonomy or "") == "disabled":
                sa.last_scheduled_at = moment
                await sa.save()
                continue
            from domains.runs.services.sweep import (
                map_areas_run_in_flight,
                run_map_areas,
            )

            # Same guard as the API endpoint: one map-areas run per repo —
            # a second would double-propose the same area tree.
            if await map_areas_run_in_flight(sa.repository_uid):
                sa.last_scheduled_at = moment
                await sa.save()
                logger.info(
                    f"schedule map-areas sa={sa.uid} expr={payload}: "
                    "already in flight — skipping tick",
                    extra={"tag": "schedule"},
                )
                continue
            # On failure, STAMP anyway: this is a monthly maintenance job —
            # retrying every beat would flood the audit log for a month. The
            # next cron slot or a manual Map-areas click is the retry path.
            try:
                mapped = await run_map_areas(
                    repository_uid=sa.repository_uid,
                    triggered_by=f"cron:{payload}",
                    trigger=RunTrigger.SCHEDULE,
                )
            except Exception as exc:  # noqa: BLE001 — one bad repo never stops the scan
                result.errors.append(f"{sa.uid}: {type(exc).__name__}: {exc}")
                sa.last_scheduled_at = moment
                await sa.save()
                logger.warning(
                    f"schedule map-areas sa={sa.uid} expr={payload} failed: "
                    f"{type(exc).__name__}: {exc} — tick consumed, next cron "
                    "slot (or a manual Map areas) retries",
                    extra={"tag": "schedule"},
                )
                continue
            if not mapped.run_uid:
                # Dispatch failed (errors captured on the result): stamp the
                # tick anyway — see the retry rationale above.
                result.errors.extend(f"{sa.uid}: {e}" for e in mapped.errors)
                sa.last_scheduled_at = moment
                await sa.save()
                logger.warning(
                    f"schedule map-areas sa={sa.uid} expr={payload} dispatched "
                    "no run — tick consumed, next cron slot (or a manual "
                    "Map areas) retries",
                    extra={"tag": "schedule"},
                )
                continue
            sa.last_scheduled_at = moment
            await sa.save()
            result.dispatched += 1
            logger.info(
                f"schedule map-areas sa={sa.uid} expr={payload} run={mapped.run_uid}",
                extra={"tag": "schedule"},
            )
            continue
        try:
            await trigger_scheduled_agent(
                sa.uid,
                trigger=RunTrigger.SCHEDULE,
                triggered_by=f"cron:{payload}",
            )
        except LifecycleError as exc:
            result.errors.append(f"{sa.uid}: {exc}")
            continue
        sa.last_scheduled_at = moment
        await sa.save()
        result.dispatched += 1
        logger.info(
            f"schedule dispatch sa={sa.uid} expr={payload}",
            extra={"tag": "schedule"},
        )
    return result
