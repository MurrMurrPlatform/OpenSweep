"""Agent CRUD, append-only revision history, and org overrides.

Tenancy: system rows (`org_uid=""`) are shared and read-only to org users —
orgs customize them through override revisions (`AgentRevision` with the
org's uid). User/imported rows belong to one org. Every mutation appends a
revision and writes an audit event.

Uniqueness of the override head is by construction: the active override for
`(agent, org)` is simply the highest-rev revision row — deletes append a
disabled tombstone revision instead of rewriting history (same monotonic
`rev` contract as the absorbed overlay system).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from domains.agents.models import (
    AGENT_PROMPT_MAX_BYTES,
    OVERRIDE_MODES,
    PRODUCES,
    REASONING_LEVELS,
    Agent,
    AgentRevision,
)
from domains.agents.schemas import (
    AgentDTO,
    AgentRevisionDTO,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from domains.agents.services.registry import (
    USER_CREATABLE_PRODUCES,
    WRITE_PRODUCES,
    agent_key,
)
from infrastructure.audit import write_audit
from logging_config import logger

_EFFORTS = {"short", "normal", "deep", "unlimited"}
# Pre-rename values still sent by old clients/seeds; normalized on write.
_EFFORT_ALIASES = {"quick": "short", "light": "short"}

# Per-process, per-(agent, org) revision locks — same pattern as the
# absorbed overlay service. Entries are never evicted; the population under
# active editing is small.
_REV_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def require_org(org_uid: str) -> str:
    org = (org_uid or "").strip()
    if not org:
        raise HTTPException(
            status_code=422,
            detail="agents are org-scoped — your user has no organization",
        )
    return org


def validate_produces(produces: str, *, allow_write: bool = False) -> str:
    p = (produces or "").strip()
    if p not in PRODUCES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown produces {produces!r}; valid: {sorted(PRODUCES)}",
        )
    allowed = USER_CREATABLE_PRODUCES | (WRITE_PRODUCES if allow_write else set())
    if p not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"produces={p!r} is reserved for system agents"
                if p not in WRITE_PRODUCES
                else "produces='code-changes' requires the maintainer role"
            ),
        )
    return p


def validate_effort(effort: str) -> str:
    e = (effort or "normal").strip().lower()
    e = _EFFORT_ALIASES.get(e, e)
    if e not in _EFFORTS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid effort {effort!r}; valid: {sorted(_EFFORTS)}",
        )
    return e


def validate_reasoning(reasoning: str) -> str:
    r = (reasoning or "").strip()
    if r not in REASONING_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid reasoning {reasoning!r}; valid: {sorted(REASONING_LEVELS)}",
        )
    return r


def _validate_body(body: str, *, field: str = "prompt") -> str:
    b = body or ""
    if len(b.encode("utf-8")) > AGENT_PROMPT_MAX_BYTES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} exceeds the {AGENT_PROMPT_MAX_BYTES // 1024} KB limit "
                f"({len(b.encode('utf-8'))} bytes) — trim it"
            ),
        )
    return b


def validate_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in OVERRIDE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid mode {mode!r}; valid: {sorted(OVERRIDE_MODES)}",
        )
    return m


def to_dto(a: Agent, *, has_org_override: bool = False) -> AgentDTO:
    return AgentDTO(
        uid=a.uid,
        title=a.title,
        description=a.description or "",
        prompt=a.prompt or "",
        produces=a.produces or "findings",
        default_effort=a.default_effort or "normal",
        reasoning=a.reasoning or "",
        tags=list(a.tags or []),
        provenance=a.provenance or "user",
        key=agent_key(a.source_url or ""),
        source_url=a.source_url or "",
        source_commit=a.source_commit or "",
        rev=int(a.rev or 0),
        has_org_override=has_org_override,
        enabled=bool(a.enabled),
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def _revision_to_dto(r: AgentRevision) -> AgentRevisionDTO:
    return AgentRevisionDTO(
        uid=r.uid,
        agent_uid=r.agent_uid,
        org_uid=r.org_uid or "",
        rev=int(r.rev or 0),
        mode=r.mode or "replace",
        body=r.body or "",
        enabled=bool(r.enabled),
        author_uid=r.author_uid or "",
        created_at=r.created_at,
    )


def _visible_to_org(a: Agent, org_uid: str) -> bool:
    return (a.org_uid or "") in ("", org_uid)


async def get_agent_model(uid: str, *, org_uid: str = "") -> Agent:
    a = await Agent.nodes.get_or_none(uid=uid)
    if a is None or (org_uid and not _visible_to_org(a, org_uid)):
        raise HTTPException(status_code=404, detail=f"Agent {uid} not found")
    return a


async def list_agents(
    *,
    org_uid: str,
    tag: Optional[str] = None,
    provenance: Optional[str] = None,
    produces: Optional[str] = None,
    enabled_only: bool = False,
) -> list[AgentDTO]:
    # Push the org-visibility filter into Cypher — Agent rows never delete
    # and this backs the Agent Library UI list, so a full-label scan grew
    # unboundedly with every tenant's user-created agents. "" = shared
    # system rows visible to every org.
    rows = list(await Agent.nodes.filter(org_uid__in=["", org_uid]))
    overridden = await _overridden_agent_uids(org_uid)
    out: list[AgentDTO] = []
    for a in rows:
        if tag and tag not in (a.tags or []):
            continue
        if provenance and (a.provenance or "") != provenance:
            continue
        if produces and (a.produces or "") != produces:
            continue
        if enabled_only and not a.enabled:
            continue
        out.append(to_dto(a, has_org_override=a.uid in overridden))
    out.sort(key=lambda d: (d.provenance != "imported", d.title.lower()))
    return out


async def _overridden_agent_uids(org_uid: str) -> set[str]:
    """Agents with an ACTIVE override for this org (latest revision enabled
    and non-empty)."""
    if not (org_uid or "").strip():
        return set()
    heads: dict[str, AgentRevision] = {}
    for r in await AgentRevision.nodes.filter(org_uid=org_uid):
        cur = heads.get(r.agent_uid)
        if cur is None or int(r.rev or 0) > int(cur.rev or 0):
            heads[r.agent_uid] = r
    return {
        uid
        for uid, r in heads.items()
        if bool(r.enabled) and (r.body or "").strip()
    }


async def get_agent(uid: str, *, org_uid: str) -> AgentDTO:
    a = await get_agent_model(uid, org_uid=org_uid)
    override = await resolve_enabled_override(org_uid, a)
    return to_dto(a, has_org_override=override is not None)


async def create_agent(
    req: CreateAgentRequest,
    *,
    org_uid: str,
    actor_uid: str = "",
    allow_write_produces: bool = False,
    provenance: str = "user",
) -> AgentDTO:
    org = require_org(org_uid)
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="agent title is required")
    a = Agent(
        uid=uuid4().hex,
        org_uid=org,
        title=title,
        description=req.description or "",
        prompt=_validate_body(req.prompt),
        produces=validate_produces(req.produces, allow_write=allow_write_produces),
        default_effort=validate_effort(req.default_effort),
        reasoning=validate_reasoning(req.reasoning),
        tags=list(req.tags or []),
        provenance=provenance,
        enabled=bool(req.enabled),
        rev=1,
    )
    await a.save()
    await _snapshot_platform_revision(a, actor_uid=actor_uid)
    await write_audit(
        kind="agent.created",
        subject_uid=a.uid,
        subject_type="Agent",
        actor_uid=actor_uid or "",
        payload={"org_uid": org, "title": title, "produces": a.produces},
    )
    return to_dto(a)


async def update_agent(
    uid: str,
    req: UpdateAgentRequest,
    *,
    org_uid: str,
    actor_uid: str = "",
    allow_write_produces: bool = False,
    platform_admin: bool = False,
) -> AgentDTO:
    a = await get_agent_model(uid, org_uid=org_uid)
    data = req.model_dump(exclude_unset=True)
    # Write-ownership gate: only the owning org may edit an agent IN PLACE. A
    # shared agent (org_uid="" — every system/imported agent) or another org's
    # agent is visible for reference but not editable here; customize it via an
    # org-scoped override (PUT /agents/{uid}/override). Without this, any tenant
    # could rewrite a globally-shared agent's prompt and it would stay shared —
    # a cross-tenant tamper of a write-capable agent. Operators bypass (they
    # curate the shared library). Provenance is a re-import marker, not an
    # authorization signal, so it is not the gate.
    owned_by_caller = bool(org_uid) and (a.org_uid or "") == org_uid
    if not owned_by_caller and not platform_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                "this agent is not owned by your org — customize it for your org "
                "via PUT /agents/{uid}/override instead of editing it in place"
            ),
        )
    if "produces" in data:
        data["produces"] = validate_produces(
            data["produces"], allow_write=allow_write_produces
        )
    if "prompt" in data:
        data["prompt"] = _validate_body(data["prompt"])
    if "default_effort" in data:
        data["default_effort"] = validate_effort(data["default_effort"])
    if "reasoning" in data:
        data["reasoning"] = validate_reasoning(data["reasoning"])
    content_fields = {"title", "description", "prompt", "produces", "tags"}
    content_changed = any(k in data for k in content_fields)
    if content_changed and (a.provenance or "") == "imported":
        # Once edited, this becomes a user agent (re-import won't clobber).
        a.provenance = "user"
    for key, value in data.items():
        setattr(a, key, value)
    a.updated_at = datetime.now(UTC)
    if content_changed:
        a.rev = int(a.rev or 0) + 1
    await a.save()
    if content_changed:
        await _snapshot_platform_revision(a, actor_uid=actor_uid)
    await write_audit(
        kind="agent.updated",
        subject_uid=a.uid,
        subject_type="Agent",
        actor_uid=actor_uid or "",
        payload={"fields": sorted(data.keys()), "rev": int(a.rev or 0)},
    )
    override = await resolve_enabled_override(org_uid, a)
    return to_dto(a, has_org_override=override is not None)


async def delete_agent(uid: str, *, org_uid: str, actor_uid: str = "") -> None:
    a = await get_agent_model(uid, org_uid=org_uid)
    if (a.provenance or "") == "system":
        raise HTTPException(
            status_code=403, detail="system agents cannot be deleted — disable instead"
        )
    from domains.agents.models import ScheduledAgent

    bindings = await ScheduledAgent.nodes.filter(agent_uid=uid)
    if bindings:
        raise HTTPException(
            status_code=409,
            detail=(
                f"agent has {len(bindings)} scheduled binding(s) — delete those "
                "first"
            ),
        )
    await a.delete()
    await write_audit(
        kind="agent.deleted",
        subject_uid=uid,
        subject_type="Agent",
        actor_uid=actor_uid or "",
        payload={"org_uid": org_uid, "title": a.title},
    )


async def _snapshot_platform_revision(a: Agent, *, actor_uid: str) -> None:
    """Append the shared/platform-body history row (org_uid="")."""
    rev = AgentRevision(
        uid=uuid4().hex,
        agent_uid=a.uid,
        org_uid="",
        rev=int(a.rev or 0),
        mode="replace",
        body=a.prompt or "",
        enabled=bool(a.enabled),
        author_uid=actor_uid or "",
    )
    await rev.save()


# ── Org overrides (the absorbed overlay system) ─────────────────────────────


async def _override_head(org_uid: str, agent_uid: str) -> AgentRevision | None:
    rows = await AgentRevision.nodes.filter(org_uid=org_uid, agent_uid=agent_uid)
    return max(rows, key=lambda r: int(r.rev or 0)) if rows else None


async def resolve_enabled_override(org_uid: str, agent: Agent) -> AgentRevision | None:
    """The ACTIVE override for composition — never raises (a broken override
    must degrade to the platform body, not fail a run)."""
    if not (org_uid or "").strip():
        return None
    try:
        head = await _override_head(org_uid, agent.uid)
    except Exception as exc:  # noqa: BLE001 — resolution failure never fails a run
        logger.warning(
            f"org override resolution failed ({org_uid}/{agent.uid}): "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "agents"},
        )
        return None
    if head is None or not head.enabled or not (head.body or "").strip():
        return None
    return head


async def active_agent_provenance(org_uid: str, agent_key_or_playbook: str) -> tuple[str, int]:
    """(agent_uid, override_rev) recorded on a Run at dispatch: the system
    agent supplying the instructions layer, and the org override revision
    active at that moment (0 = platform body as-is). Never raises."""
    from domains.agents.services.registry import system_agent_by_key

    try:
        agent = await system_agent_by_key(agent_key_or_playbook)
        if agent is None:
            return "", 0
        override = await resolve_enabled_override(org_uid, agent)
        return agent.uid, int(override.rev or 0) if override else 0
    except Exception as exc:  # noqa: BLE001 — provenance must never block a run
        logger.warning(
            f"agent provenance lookup failed ({org_uid}/{agent_key_or_playbook}): {exc}",
            extra={"tag": "agents"},
        )
        return "", 0


async def _next_override_rev(org_uid: str, agent_uid: str) -> int:
    head = await _override_head(org_uid, agent_uid)
    return (int(head.rev or 0) if head else 0) + 1


async def save_override(
    *,
    agent_uid: str,
    org_uid: str,
    mode: str,
    body: str,
    enabled: bool = True,
    actor_uid: str = "",
    audit_kind: str = "agent_override.updated",
    audit_extra: dict | None = None,
) -> AgentRevisionDTO:
    org = require_org(org_uid)
    a = await get_agent_model(agent_uid)
    if (a.provenance or "") != "system":
        raise HTTPException(
            status_code=422,
            detail="overrides apply to system agents — edit your own agent directly",
        )
    m = validate_mode(mode)
    b = _validate_body(body, field="override body")
    lock = _REV_LOCKS.setdefault((agent_uid, org), asyncio.Lock())
    async with lock:
        revision = AgentRevision(
            uid=uuid4().hex,
            agent_uid=agent_uid,
            org_uid=org,
            rev=await _next_override_rev(org, agent_uid),
            mode=m,
            body=b,
            enabled=bool(enabled),
            author_uid=actor_uid or "",
        )
        await revision.save()
    await write_audit(
        kind=audit_kind,
        subject_uid=agent_uid,
        subject_type="Agent",
        actor_uid=actor_uid or "",
        payload={
            "org_uid": org,
            "mode": m,
            "enabled": bool(enabled),
            "rev": int(revision.rev),
            "body_bytes": len((b or "").encode("utf-8")),
            **(audit_extra or {}),
        },
    )
    return _revision_to_dto(revision)


async def delete_override(*, agent_uid: str, org_uid: str, actor_uid: str = "") -> None:
    """Restore the platform default: append a disabled tombstone revision.
    History is kept and the monotonic rev sequence continues."""
    org = require_org(org_uid)
    head = await _override_head(org, agent_uid)
    if head is None or (not head.enabled and not (head.body or "").strip()):
        raise HTTPException(status_code=404, detail="no override set for this agent")
    await save_override(
        agent_uid=agent_uid,
        org_uid=org,
        mode="append",
        body="",
        enabled=False,
        actor_uid=actor_uid,
        audit_kind="agent_override.deleted",
    )


async def list_revisions(
    agent_uid: str, *, org_uid: str, include_platform: bool = True
) -> list[AgentRevisionDTO]:
    """The agent's revision history visible to this org: its own override
    revisions plus (optionally) the shared platform-body history."""
    await get_agent_model(agent_uid, org_uid=org_uid)
    rows = list(await AgentRevision.nodes.filter(agent_uid=agent_uid, org_uid=org_uid))
    if include_platform:
        rows += list(await AgentRevision.nodes.filter(agent_uid=agent_uid, org_uid=""))
    rows.sort(key=lambda r: ((r.org_uid or "") != "", int(r.rev or 0)), reverse=True)
    return [_revision_to_dto(r) for r in rows]


async def revert_override(
    *, agent_uid: str, org_uid: str, rev: int, actor_uid: str = ""
) -> AgentRevisionDTO:
    """Revert = save a NEW head revision copying an old one (append-only)."""
    org = require_org(org_uid)
    revisions = await AgentRevision.nodes.filter(agent_uid=agent_uid, org_uid=org)
    source = next((r for r in revisions if int(r.rev or 0) == int(rev)), None)
    if source is None:
        raise HTTPException(status_code=404, detail=f"revision {rev} not found")
    return await save_override(
        agent_uid=agent_uid,
        org_uid=org,
        mode=source.mode or "append",
        body=source.body or "",
        enabled=True,
        actor_uid=actor_uid,
        audit_kind="agent_override.reverted",
        audit_extra={"reverted_to_rev": int(rev)},
    )
