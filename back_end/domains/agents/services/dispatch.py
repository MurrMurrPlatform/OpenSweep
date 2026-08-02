"""Dispatch bridge: Agent / ScheduledAgent → runs.lifecycle.trigger_run.

The ONLY module that turns agent configuration into an actual Run. It
composes the intent (system base or user prompt + org override + repo
stage guidance + target scope), maps `produces` onto the internal playbook,
resolves effort → run policy, and stamps provenance
(`scheduled_agent_uid`, `agent_uid`, `agent_rev`) on the Run.

Write agents (produces="code-changes") are refused outright: write runs
need a prepared write sandbox and ticket/PR context, which only the
delivery/thread services provide.
"""

from __future__ import annotations

from typing import Any

from domains.agents.models import Agent, ScheduledAgent
from domains.agents.services.composition import compose_agent_intent
from domains.agents.services.registry import (
    agent_key,
    playbook_for_produces,
    stage_for_agent_key,
)
from domains.runs.schemas import Effort, RunTrigger, normalize_effort, resolve_reasoning


async def _resolve_scope_docs(
    repository_uid: str, doc_uids: list[str]
) -> dict[str, tuple[str, str, bool, bool]]:
    """{doc_uid: (slug, title, stale, archived)} for the scoped pages.

    The scope block used to print bare uids, but `read_doc` is keyed by SLUG
    and no uid-keyed read tool exists — so the agent had to reverse the uids
    through `list_docs` before it could open anything. Resolve them here.

    Archived pages are fetched rather than filtered out: a retired page sitting
    in an Area's doc_uids should be named and marked, not silently dropped from
    a scope the caller asked for.
    """
    from domains.docs.models import Doc, doc_is_stale

    if not doc_uids:
        return {}
    return {
        d.uid: (
            d.slug or "",
            d.title or d.slug or "",
            doc_is_stale(d),
            bool(d.archived),
        )
        for d in await Doc.nodes.filter(
            repository_uid=repository_uid, uid__in=list(dict.fromkeys(doc_uids))
        )
    }


def _scope_summary(
    target: dict[str, Any],
    scope_docs: dict[str, tuple[str, str, bool, bool]] | None = None,
) -> str:
    """Human-readable scope block for the composed intent."""
    parts: list[str] = []
    scope_docs = scope_docs or {}
    paths = [str(p) for p in (target.get("paths") or []) if str(p).strip()]
    doc_uids = [str(d) for d in (target.get("doc_uids") or []) if str(d).strip()]
    if paths:
        parts.append("Limit the investigation to these repository paths:\n" +
                      "\n".join(f"- {p}" for p in paths))
    if doc_uids:
        lines: list[str] = []
        for uid in doc_uids:
            resolved = scope_docs.get(uid)
            if resolved is None:
                # Deleted between dispatch and compose, or an unresolvable uid.
                lines.append(f"- (unknown page uid={uid})")
                continue
            slug, title, stale, archived = resolved
            if archived:
                flag = " — ARCHIVED, skip it"
            elif stale:
                flag = " — STALE, code changed since last review"
            else:
                flag = ""
            lines.append(f"- {slug} ({title}){flag}")
        parts.append(
            "Scope: these documentation pages — read each with "
            "`read_doc(slug=…)`, and finish each as either `propose_doc_edit` "
            "or `confirm_doc_current(slug=…)`:\n" + "\n".join(lines)
        )
    if not parts:
        parts.append("Scope: the whole repository.")
    return "\n\n".join(parts)


async def dispatch_agent(
    *,
    agent: Agent,
    repository_uid: str,
    target: dict[str, Any] | None = None,
    effort: str = "",
    run_policy_uid: str | None = None,
    scheduled_agent_uid: str = "",
    trigger: RunTrigger = RunTrigger.MANUAL,
    triggered_by: str = "",
    title: str = "",
    # Extra code-owned structural material appended after the scope block
    # (e.g. a campaign's escalation digest) — same slot, never displaceable.
    structural_extra: str = "",
):
    """Compose and dispatch one run of an Agent on a repository."""
    from domains.run_policies.services.effort import ensure_policy_for_effort
    from domains.runs.services.lifecycle import LifecycleError, trigger_run

    if (agent.produces or "") == "code-changes":
        raise LifecycleError(
            "code-changes agents cannot be dispatched directly — write runs "
            "flow through the delivery pipeline"
        )
    if not agent.enabled:
        raise LifecycleError(f"Agent {agent.uid} is disabled")

    key = agent_key(agent.source_url or "")
    playbook = playbook_for_produces(agent.produces or "findings")
    target = dict(target or {})

    is_system = (agent.provenance or "") == "system" and key
    scope_docs = await _resolve_scope_docs(
        repository_uid, [str(d) for d in (target.get("doc_uids") or []) if str(d).strip()]
    )
    structural = _scope_summary(target, scope_docs)
    if structural_extra.strip():
        structural = f"{structural}\n\n{structural_extra.strip()}"
    composed = await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key=key if is_system else playbook,
        # A user/imported agent's own prompt takes the instructions slot; a
        # system agent's prompt IS the platform base and resolves by key.
        prompt_body=None if is_system else (agent.prompt or ""),
        structural=structural,
    )

    resolved_effort = (effort or agent.default_effort or "normal").strip()
    tier = normalize_effort(resolved_effort)
    policy_uid = run_policy_uid
    if not policy_uid:
        policy = await ensure_policy_for_effort(tier)
        policy_uid = policy.uid

    return await trigger_run(
        repository_uid=repository_uid,
        intent=composed.text,
        playbook=playbook,
        title=title or agent.title,
        target=target,
        run_policy_uid=policy_uid,
        effort=tier.value,
        reasoning=resolve_reasoning(agent.reasoning or "", tier),
        trigger=trigger,
        triggered_by=triggered_by,
        scheduled_agent_uid=scheduled_agent_uid,
        agent_uid=agent.uid,
        agent_rev=composed.agent_rev,
        stage=stage_for_agent_key(key, playbook),
        composed_degraded=composed.composed_degraded,
        degraded_layers=composed.degraded_layers,
    )


async def trigger_scheduled_agent(
    scheduled_agent_uid: str,
    *,
    trigger: RunTrigger = RunTrigger.MANUAL,
    triggered_by: str = "",
):
    """Dispatch one run for a ScheduledAgent binding."""
    from domains.runs.services.lifecycle import LifecycleError

    s = await ScheduledAgent.nodes.get_or_none(uid=scheduled_agent_uid)
    if s is None:
        raise LifecycleError(f"ScheduledAgent {scheduled_agent_uid} not found")
    agent = await Agent.nodes.get_or_none(uid=s.agent_uid)
    if agent is None:
        raise LifecycleError(f"Agent {s.agent_uid} (bound by {s.uid}) not found")
    if agent_key(agent.source_url or "") == "map-areas":
        # Map-areas bindings dispatch through the sweep flow so the run gets
        # the existing-areas + doc-tree listings and the propose_area_edit
        # tooling contract — a plain dispatch_agent would compose neither.
        # Callers expect a Run (or an exception), so surface failures as
        # LifecycleError and hand back the dispatched Run node.
        from domains.runs.models import Run
        from domains.runs.services.sweep import run_map_areas

        result = await run_map_areas(
            repository_uid=s.repository_uid,
            triggered_by=triggered_by,
            trigger=trigger,
        )
        if not result.run_uid:
            raise LifecycleError(
                "map-areas dispatch failed: "
                + ("; ".join(result.errors) or "no run dispatched")
            )
        run = await Run.nodes.get_or_none(uid=result.run_uid)
        if run is None:
            raise LifecycleError(
                f"map-areas run {result.run_uid} dispatched but not found"
            )
        return run
    return await dispatch_agent(
        agent=agent,
        repository_uid=s.repository_uid,
        target=dict(s.target or {}),
        effort=s.effort or "",
        run_policy_uid=s.run_policy_uid or None,
        scheduled_agent_uid=s.uid,
        trigger=trigger,
        triggered_by=triggered_by,
        title=s.title or agent.title,
    )
