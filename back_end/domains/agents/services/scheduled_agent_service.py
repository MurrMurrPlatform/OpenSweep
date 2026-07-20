"""ScheduledAgent CRUD + per-repository seeding.

Every repository gets two seeded bindings on registration:

- "Keep docs current" — the `document` system agent, on-event, repo-wide,
  dial `suggest` (the user dials it up to auto-run-cheap/any to make every
  push refresh the wiki).
- "Audit stale code" — the `audit-stale` system agent, seeded INERT
  (trigger="") with target {limit: 3}; a user-set cron is the opt-in.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from domains.agents.models import COMPUTE_DIALS, Agent, ScheduledAgent
from domains.agents.schemas import (
    CreateScheduledAgentRequest,
    ScheduledAgentDTO,
    UpdateScheduledAgentRequest,
    parse_trigger,
)
from domains.agents.services.registry import agent_key, system_agent_by_key
from infrastructure.audit import write_audit
from logging_config import logger

KEEP_DOCS_CURRENT_TITLE = "Keep docs current"
AUDIT_STALE_TITLE = "Audit stale code"

# Recurring whole-repo audit bindings seeded on every repository. The weekly
# hunts are enabled by default; the daily hunt is seeded disabled (opt-in) as
# it is the higher-frequency, higher-cost sweep.
DEEP_ISSUE_HUNT_KEY = "deep-issue-hunt"
SECURITY_AUDIT_KEY = "security-audit"
DEEP_HUNT_WEEKLY_TITLE = "Weekly FULL deep issue hunt (Mon)"
DEEP_HUNT_DAILY_TITLE = "Daily deep issue hunt (Tue–Sat)"
SECURITY_AUDIT_WEEKLY_TITLE = "Weekly security audit (Mon)"


def validate_trigger(trigger: str) -> str:
    raw = (trigger or "").strip()
    try:
        kind, expr = parse_trigger(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if kind == "cron":
        from croniter import croniter

        if not croniter.is_valid(expr):
            raise HTTPException(status_code=422, detail=f"invalid crontab: {expr!r}")
    return raw


def validate_dial(dial: str) -> str:
    d = (dial or "").strip()
    if d not in COMPUTE_DIALS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid compute_dial {dial!r}; valid: {sorted(COMPUTE_DIALS)}",
        )
    return d


async def to_dto(s: ScheduledAgent, *, agent: Agent | None = None) -> ScheduledAgentDTO:
    a = agent
    if a is None or a.uid != s.agent_uid:
        a = await Agent.nodes.get_or_none(uid=s.agent_uid)
    return ScheduledAgentDTO(
        uid=s.uid,
        agent_uid=s.agent_uid,
        repository_uid=s.repository_uid,
        title=s.title or "",
        trigger=s.trigger or "",
        target=dict(s.target or {}),
        effort=s.effort or "",
        run_policy_uid=s.run_policy_uid or None,
        compute_dial=s.compute_dial or "ask-before-run",
        enabled=bool(s.enabled),
        provenance=s.provenance or "user",
        last_scheduled_at=s.last_scheduled_at,
        agent_title=(a.title if a else ""),
        agent_produces=(a.produces or "findings") if a else "findings",
        agent_key=agent_key(a.source_url or "") if a else "",
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def list_scheduled_agents(
    *, repository_uid: Optional[str] = None
) -> list[ScheduledAgentDTO]:
    rows = await ScheduledAgent.nodes.all()
    if repository_uid:
        rows = [s for s in rows if s.repository_uid == repository_uid]
    rows.sort(key=lambda s: ((s.provenance or "") != "system", (s.title or "").lower()))
    agents = {a.uid: a for a in await Agent.nodes.all()}
    return [await to_dto(s, agent=agents.get(s.agent_uid)) for s in rows]


async def get_scheduled_agent_model(uid: str) -> ScheduledAgent:
    s = await ScheduledAgent.nodes.get_or_none(uid=uid)
    if s is None:
        raise HTTPException(status_code=404, detail=f"ScheduledAgent {uid} not found")
    return s


async def create_scheduled_agent(
    req: CreateScheduledAgentRequest, *, actor_uid: str = ""
) -> ScheduledAgentDTO:
    agent = await Agent.nodes.get_or_none(uid=req.agent_uid)
    if agent is None or not agent.enabled:
        raise HTTPException(
            status_code=422, detail=f"Agent {req.agent_uid} not found or disabled"
        )
    _reject_write_agent(agent)
    s = ScheduledAgent(
        uid=uuid4().hex,
        agent_uid=req.agent_uid,
        repository_uid=req.repository_uid,
        title=req.title or "",
        trigger=validate_trigger(req.trigger),
        target=dict(req.target or {}),
        effort=req.effort or "",
        run_policy_uid=req.run_policy_uid or "",
        compute_dial=validate_dial(req.compute_dial),
        enabled=bool(req.enabled),
        provenance="user",
    )
    await s.save()
    await write_audit(
        kind="scheduled_agent.created",
        subject_uid=s.uid,
        subject_type="ScheduledAgent",
        actor_uid=actor_uid or "",
        payload={
            "agent_uid": s.agent_uid,
            "repository_uid": s.repository_uid,
            "trigger": s.trigger,
            "compute_dial": s.compute_dial,
        },
    )
    return await to_dto(s, agent=agent)


def _reject_write_agent(agent: Agent) -> None:
    if (agent.produces or "") == "code-changes":
        raise HTTPException(
            status_code=422,
            detail=(
                "code-changes agents cannot be scheduled — write runs need a "
                "prepared write sandbox and ticket/PR context, and are "
                "dispatched by the delivery flow"
            ),
        )


async def update_scheduled_agent(
    uid: str, req: UpdateScheduledAgentRequest, *, actor_uid: str = ""
) -> ScheduledAgentDTO:
    s = await get_scheduled_agent_model(uid)
    data = req.model_dump(exclude_unset=True)
    if "trigger" in data and data["trigger"] is not None:
        data["trigger"] = validate_trigger(data["trigger"])
    if "compute_dial" in data and data["compute_dial"] is not None:
        data["compute_dial"] = validate_dial(data["compute_dial"])
    for key, value in data.items():
        if value is None:
            continue
        setattr(s, key, value)
    from datetime import UTC, datetime

    s.updated_at = datetime.now(UTC)
    await s.save()
    await write_audit(
        kind="scheduled_agent.updated",
        subject_uid=s.uid,
        subject_type="ScheduledAgent",
        actor_uid=actor_uid or "",
        payload={"fields": sorted(data.keys())},
    )
    return await to_dto(s)


async def delete_scheduled_agent(uid: str, *, actor_uid: str = "") -> None:
    s = await get_scheduled_agent_model(uid)
    await s.delete()
    await write_audit(
        kind="scheduled_agent.deleted",
        subject_uid=uid,
        subject_type="ScheduledAgent",
        actor_uid=actor_uid or "",
        payload={"repository_uid": s.repository_uid, "title": s.title or ""},
    )


# ── Per-repository seeding ──────────────────────────────────────────────────


async def seed_keep_docs_current(repository_uid: str) -> ScheduledAgent | None:
    """Idempotent: one seeded docs-freshness binding per repository."""
    agent = await system_agent_by_key("document")
    if agent is None:
        logger.warning(
            "document system agent missing — skipping keep-docs-current seed",
            extra={"tag": "seeding"},
        )
        return None
    for s in await ScheduledAgent.nodes.filter(repository_uid=repository_uid):
        if s.title == KEEP_DOCS_CURRENT_TITLE and s.agent_uid == agent.uid:
            return None
    s = ScheduledAgent(
        uid=uuid4().hex,
        agent_uid=agent.uid,
        repository_uid=repository_uid,
        title=KEEP_DOCS_CURRENT_TITLE,
        trigger="on-event",
        target={},  # empty = repo-wide: any change makes it a candidate
        compute_dial="suggest",
        provenance="system",
    )
    await s.save()
    return s


async def seed_audit_stale(repository_uid: str) -> ScheduledAgent | None:
    """Idempotent: one seeded stale-audit binding per repository, INERT
    (trigger="") — a user-set cron is the opt-in, matching the scanner's
    semantics. Each due tick runs sweep.run_auto_audit: rank pages
    never-checked first then longest-stale, dispatch one scoped audit per
    page up to target.limit."""
    agent = await system_agent_by_key("audit-stale")
    if agent is None:
        logger.warning(
            "audit-stale system agent missing — skipping audit-stale seed",
            extra={"tag": "seeding"},
        )
        return None
    for s in await ScheduledAgent.nodes.filter(repository_uid=repository_uid):
        if s.title == AUDIT_STALE_TITLE and s.agent_uid == agent.uid:
            return None
    s = ScheduledAgent(
        uid=uuid4().hex,
        agent_uid=agent.uid,
        repository_uid=repository_uid,
        title=AUDIT_STALE_TITLE,
        trigger="",
        target={"limit": 3},
        compute_dial="ask-before-run",
        provenance="system",
    )
    await s.save()
    return s


async def _seed_binding(
    repository_uid: str,
    *,
    key: str,
    title: str,
    trigger: str,
    enabled: bool,
) -> ScheduledAgent | None:
    """Idempotent one-off: bind system agent `key` to a repo on `trigger`.

    Skips (returns None) if the agent is missing or a same-title binding for
    that agent already exists on the repo — so re-running never duplicates and
    never clobbers a user's later edits (enable/dial/cron changes).
    """
    agent = await system_agent_by_key(key)
    if agent is None:
        logger.warning(
            "%s system agent missing — skipping %r seed",
            key,
            title,
            extra={"tag": "seeding"},
        )
        return None
    for s in await ScheduledAgent.nodes.filter(repository_uid=repository_uid):
        if s.title == title and s.agent_uid == agent.uid:
            return None
    s = ScheduledAgent(
        uid=uuid4().hex,
        agent_uid=agent.uid,
        repository_uid=repository_uid,
        title=title,
        trigger=trigger,
        target={},  # empty = repo-wide
        compute_dial="ask-before-run",
        enabled=enabled,
        provenance="system",
    )
    await s.save()
    return s


async def seed_audit_agents(repository_uid: str) -> list[ScheduledAgent]:
    """Idempotent: the recurring whole-repo audit bindings every repo gets.

    - Weekly FULL deep issue hunt (Mondays 06:00)  — enabled.
    - Weekly security audit (Mondays 08:00)         — enabled.
    - Daily deep issue hunt (Tue–Sat 06:00)         — seeded DISABLED; flip
      `enabled` on to opt into the higher-frequency (higher-cost) daily sweep.

    All seed with compute_dial="ask-before-run": an enabled binding's cron tick
    proposes a run for approval rather than auto-billing. Dial up to
    auto-run-cheap/auto-run-any for unattended operation.
    """
    seeded: list[ScheduledAgent] = []
    for key, title, trigger, enabled in (
        (DEEP_ISSUE_HUNT_KEY, DEEP_HUNT_WEEKLY_TITLE, "cron:0 6 * * 1", True),
        (SECURITY_AUDIT_KEY, SECURITY_AUDIT_WEEKLY_TITLE, "cron:0 8 * * 1", True),
        (DEEP_ISSUE_HUNT_KEY, DEEP_HUNT_DAILY_TITLE, "cron:0 6 * * 2-6", False),
    ):
        s = await _seed_binding(
            repository_uid, key=key, title=title, trigger=trigger, enabled=enabled
        )
        if s is not None:
            seeded.append(s)
    return seeded
