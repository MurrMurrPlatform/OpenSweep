"""Dispatch one campaign part as a Run.

Area parts mirror sweep.run_audit's compose+trigger call: the seeded "ask"
base is the instructions layer, and the code-owned structural slot carries
the scope contract, the rendered lens checklist, and the coverage
reporting contract (complete_run's covered/skipped/lens_verdicts). Feature
parts are area parts with the Area's spec inlined as the contract to
verify (implementation-gaps lens). Global parts dispatch the lens's seeded
variant Agent with a digest of the `escalate:<lens-key>` findings the area
runs filed for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from domains.agents.services.composition import compose_agent_intent
from domains.agents.services.dispatch import dispatch_agent
from domains.agents.services.registry import system_agent_by_url, variant_source_url
from domains.areas.services import area_service
from domains.lenses.services import lens_service
from domains.lenses.services.lens_service import lens_checklist
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.runs.schemas import RunTrigger, normalize_effort
from domains.runs.services.lifecycle import LifecycleError, trigger_run
from infrastructure.audit import write_audit
from logging_config import logger

# How many escalated findings a global sweep's digest carries.
_MAX_ESCALATIONS = 20


def _campaign_trigger(campaign) -> RunTrigger:
    provenance = campaign.trigger_provenance or ""
    return RunTrigger.SCHEDULE if provenance.startswith("cron:") else RunTrigger.MANUAL


def _scope_contract(campaign, part: dict) -> str:
    total = len(campaign.parts or [])
    lines = [
        f"You are running part {int(part['idx']) + 1} of {total} of audit "
        f"campaign '{campaign.title}'. Your scope is ONLY these paths:"
    ]
    lines += [f"- {p}" for p in (part.get("scope_paths") or [])]
    lines.append("Do not investigate outside this scope.")
    keys = [str(k) for k in (part.get("area_keys") or [])]
    if len(keys) > 1:
        # Bundled sibling areas share this run — none may be skipped.
        lines.append(
            f"This part covers areas: {', '.join(keys)} — audit all of them."
        )
    return "\n".join(lines)


_REPORTING_CONTRACT = (
    "When done, call complete_run with covered_paths (paths you actually "
    "examined), skipped_paths (in-scope paths you did not), and "
    "lens_verdicts — one entry per lens above "
    "({lens, verdict: checked-clean|checked-findings|skipped, note})."
)


def _target(campaign, part: dict, **extra: Any) -> dict[str, Any]:
    return {
        "paths": list(part.get("scope_paths") or []),
        "doc_uids": list(part.get("doc_uids") or []),
        "campaign_uid": campaign.uid,
        "campaign_part": int(part["idx"]),
        "area_keys": [str(k) for k in (part.get("area_keys") or [])],
        **extra,
    }


async def _escalation_digest(repository_uid: str, lens_key: str) -> list[str]:
    """Open findings the area runs escalated to this lens, as digest lines."""
    from domains.findings.models import Finding

    tag = f"escalate:{lens_key}"
    rows = [
        f
        for f in await Finding.nodes.filter(repository_uid=repository_uid)
        if (f.status or "") == "open" and tag in (f.tags or [])
    ]
    rows.sort(key=lambda f: f.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return [
        f"- {f.title} ({(list(f.affected_paths or []) or [''])[0]})"
        for f in rows[:_MAX_ESCALATIONS]
    ]


async def _dispatch_area(campaign, part: dict, *, spec_block: str = "") -> str:
    lenses = [await lens_service.get_by_key(k) for k in (part.get("lens_keys") or [])]
    blocks = [_scope_contract(campaign, part), lens_checklist(lenses)]
    if spec_block:
        blocks.append(spec_block)
    blocks.append(_REPORTING_CONTRACT)
    structural = "\n\n".join(blocks)
    composed = await compose_agent_intent(
        repository_uid=campaign.repository_uid,
        agent_key="ask",
        prompt_body=None,
        structural=structural,
    )
    tier = normalize_effort(campaign.effort or "normal")
    policy = await ensure_policy_for_effort(tier)
    run = await trigger_run(
        repository_uid=campaign.repository_uid,
        intent=composed.text,
        playbook="ask",
        title=f"Campaign: {part.get('title') or 'area'}",
        target=_target(campaign, part),
        run_policy_uid=policy.uid,
        effort=tier.value,
        agent_uid=composed.agent_uid,
        agent_rev=composed.agent_rev,
        trigger=_campaign_trigger(campaign),
        triggered_by=campaign.created_by or "campaign",
    )
    return run.uid


async def _dispatch_feature(campaign, part: dict) -> str:
    """A feature part is an area part anchored to its Area's spec: the spec
    block sits between the lens checklist and the reporting contract. The
    Area is loaded FRESH at dispatch time so the run verifies today's
    contract, not the plan-time snapshot. Missing/disabled area or empty
    spec degrades to a plain area dispatch — NEVER raises: part states are
    sticky (tick.plan_tick), so a raise here would fail the part forever
    over a fixable map edit. Degradation is made visible: an audit event is
    written and the run's structural carries a note telling the agent the
    spec contract could not be verified."""
    keys = [str(k) for k in (part.get("area_keys") or [])]
    # Feature parts are never bundled — one Area per part ([0]); an empty
    # list degrades exactly like a deleted area.
    area_key = keys[0] if keys else ""
    area = (
        await area_service.get_area_by_key(campaign.repository_uid, area_key)
        if area_key
        else None
    )
    spec = (area.spec or "").strip() if area is not None else ""
    if area is None or not bool(area.enabled) or not spec:
        reason = (
            "not found"
            if area is None
            else ("disabled" if not bool(area.enabled) else "has no spec")
        )
        logger.warning(
            f"campaign {campaign.uid} part {part.get('idx')}: feature area "
            f"{area_key!r} {reason} — dispatching as a plain area part",
            extra={"tag": "campaigns"},
        )
        await write_audit(
            kind="campaign.feature_part_degraded",
            subject_uid=campaign.uid,
            subject_type="Campaign",
            repository_uid=campaign.repository_uid,
            actor_uid="campaign",
            payload={
                "part": part.get("idx"),
                "area_key": area_key,
                "reason": reason,
            },
        )
        spec_block = (
            "## Note — feature spec unavailable\n"
            f"This part was planned as a feature-spec audit of area "
            f"'{area_key}', but that area {reason}. Audit the scope with the "
            "lens checklist as a normal sweep; the spec contract could NOT "
            "be verified — say so in your report."
        )
        return await _dispatch_area(campaign, part, spec_block=spec_block)
    spec_block = (
        "## Feature spec — verify the implementation matches this contract "
        "end-to-end\n\n" + spec
    )
    return await _dispatch_area(campaign, part, spec_block=spec_block)


async def _dispatch_global(campaign, part: dict) -> str:
    keys = list(part.get("lens_keys") or [])
    if not keys:
        raise LifecycleError(f"global part {part.get('idx')} has no lens key")
    lens = await lens_service.get_by_key(keys[0])
    variant = await system_agent_by_url(
        variant_source_url(lens.global_agent_key or lens.key)
    )
    if variant is None:
        raise LifecycleError(
            f"no seeded variant agent for global lens {lens.key!r} "
            f"(slug {lens.global_agent_key or lens.key!r})"
        )
    digest = await _escalation_digest(campaign.repository_uid, lens.key)
    structural_extra = ""
    if digest:
        structural_extra = (
            "Escalated observations from this campaign's area runs — verify "
            "each against the current code:\n" + "\n".join(digest)
        )
    prefix = str(getattr(campaign, "area_prefix", "") or "")
    if prefix:
        # The sweep agent stays whole-repo; steer it toward the slice this
        # campaign covers (scope_hint = union of the filtered areas' scopes).
        hint = ", ".join(part.get("scope_hint") or []) or prefix
        scope_note = (
            f"This campaign is scoped to areas under '{prefix}'. "
            f"Concentrate on: {hint}."
        )
        structural_extra = (
            f"{structural_extra}\n\n{scope_note}" if structural_extra else scope_note
        )
    run = await dispatch_agent(
        agent=variant,
        repository_uid=campaign.repository_uid,
        target=_target(campaign, part, escalations=digest),
        # Global sweeps are whole-repo: default deep unless the campaign
        # pinned an explicit tier for its children.
        effort=campaign.effort or "deep",
        trigger=_campaign_trigger(campaign),
        triggered_by=campaign.created_by or "campaign",
        title=f"Campaign: {part.get('title') or lens.key}",
        structural_extra=structural_extra,
    )
    return run.uid


async def dispatch_part(campaign, part: dict) -> str:
    """Dispatch one pending part; returns the child run's uid."""
    kind = part.get("kind") or "area"
    if kind == "global":
        return await _dispatch_global(campaign, part)
    if kind == "feature":
        return await _dispatch_feature(campaign, part)
    return await _dispatch_area(campaign, part)
