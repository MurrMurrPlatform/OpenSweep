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
from domains.executors.prompt_kit import coverage_contract
from domains.lenses.services import lens_service
from domains.lenses.services.lens_service import lens_checklist
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.runs.schemas import RunTrigger, normalize_effort
from domains.runs.services.lifecycle import LifecycleError, trigger_run
from infrastructure.audit import write_audit
from logging_config import logger

# How many escalated findings a global sweep's digest carries.
_MAX_ESCALATIONS = 20

# Evidence key recording which runs have already been handed this escalation.
# See `_escalation_digest` for why delivery is ranked rather than consumed.
_DELIVERED_KEY = "escalation_delivered_to"


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




def _target(campaign, part: dict, **extra: Any) -> dict[str, Any]:
    return {
        "paths": list(part.get("scope_paths") or []),
        "doc_uids": list(part.get("doc_uids") or []),
        "campaign_uid": campaign.uid,
        "campaign_part": int(part["idx"]),
        "area_keys": [str(k) for k in (part.get("area_keys") or [])],
        # The lenses this part was assigned. Carried so the dispatch side can
        # honour `Lens.wants` (lifecycle._run_wants_static_analysis) instead
        # of running every analyzer for every run regardless of what the
        # lenses actually asked for.
        "lens_keys": [str(k) for k in (part.get("lens_keys") or [])],
        **extra,
    }


async def _escalation_digest(repository_uid: str, lens_key: str) -> tuple[list[str], list]:
    """Open findings the area runs escalated to this lens → (digest lines, rows).

    Ranks NEVER-DELIVERED escalations first, then previously-delivered ones
    oldest-delivery-first, and only then by age. The queue was sorted purely
    newest-first and truncated at _MAX_ESCALATIONS with nothing ever marking
    an item handled, so on a repo with more than 20 open escalations the same
    newest 20 rode every sweep forever and items 21+ were never seen at all.

    Delivery is RANKED, not consumed — the same argument
    `audit_selection.coverage_recency_for` makes about coverage stamps. A
    hard "consumed" flag would drop an escalation permanently the first time
    a sweep was handed it, including when that sweep then failed; ranking
    rotates the queue without ever losing an item. The digest is built at
    dispatch, so delivery is exactly what we can honestly claim.
    """
    from domains.findings.models import Finding

    _EPOCH = datetime.min.replace(tzinfo=UTC)
    tag = f"escalate:{lens_key}"
    rows = [
        f
        for f in await Finding.nodes.filter(repository_uid=repository_uid)
        if (f.status or "") == "open" and tag in (f.tags or [])
    ]

    def _delivery_count(f) -> int:
        return len(((f.evidence or {}).get(_DELIVERED_KEY)) or [])

    rows.sort(key=lambda f: (_delivery_count(f), -(f.created_at or _EPOCH).timestamp()))
    selected = rows[:_MAX_ESCALATIONS]
    lines = [
        f"- {f.title} ({(list(f.affected_paths or []) or [''])[0]})" for f in selected
    ]
    if len(rows) > len(selected):
        logger.info(
            f"escalation digest for lens {lens_key!r} truncated: "
            f"{len(selected)} of {len(rows)} open escalations carried "
            f"(least-recently-delivered first)",
            extra={"tag": "campaigns"},
        )
    return lines, selected


async def _mark_escalations_delivered(rows: list, run_uid: str) -> None:
    """Record that these escalations rode a sweep's digest. Best-effort: a
    failure here costs rotation fairness, never the run."""
    for f in rows:
        try:
            evidence = dict(f.evidence or {})
            delivered = [str(u) for u in (evidence.get(_DELIVERED_KEY) or [])]
            if run_uid in delivered:
                continue
            evidence[_DELIVERED_KEY] = [*delivered, run_uid]
            f.evidence = evidence
            await f.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"could not stamp escalation delivery on finding "
                f"{getattr(f, 'uid', '?')}: {type(exc).__name__}: {exc}",
                extra={"tag": "campaigns"},
            )


async def _dispatch_area(campaign, part: dict, *, spec_block: str = "") -> str:
    lenses = [await lens_service.get_by_key(k) for k in (part.get("lens_keys") or [])]
    blocks = [_scope_contract(campaign, part), lens_checklist(lenses)]
    if spec_block:
        blocks.append(spec_block)
    blocks.append(coverage_contract(lens_keys=part.get("lens_keys") or []))
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
        composed_degraded=composed.composed_degraded,
        degraded_layers=composed.degraded_layers,
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
        if reason == "has no spec":
            # Visible, actionable signal (not just a silent degrade): the
            # feature exists but was never spec'd, so its contract could not
            # be verified. This kind maps to the `feature.spec_missing`
            # notification (catalog) — run generate-specs to fix it.
            await write_audit(
                kind="campaign.feature_no_spec",
                subject_uid=campaign.uid,
                subject_type="Campaign",
                repository_uid=campaign.repository_uid,
                actor_uid="campaign",
                payload={
                    "part": part.get("idx"),
                    "area_key": area_key,
                    "campaign_title": campaign.title or "",
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
    digest, escalated = await _escalation_digest(campaign.repository_uid, lens.key)
    structural_extra = ""
    if digest:
        structural_extra = (
            "Escalated observations from this campaign's area runs — verify "
            "each against the current code:\n" + "\n".join(digest)
        )
    prefix = str(getattr(campaign, "area_prefix", "") or "")
    coverage_keys = [str(k) for k in (getattr(campaign, "coverage_keys", []) or [])]
    # Gate on EITHER path: the legacy area_prefix or the newer coverage_keys.
    # `_plan_parts` writes scope_hint whenever coverage_keys resolves any
    # areas; without this gate a coverage_keys-only global campaign had its
    # hint computed and then silently never read at dispatch time.
    if prefix or coverage_keys:
        # The sweep agent stays whole-repo; steer it toward the slice this
        # campaign covers (scope_hint = union of the filtered areas' scopes).
        scope_hint = part.get("scope_hint") or []
        hint = ", ".join(scope_hint) or prefix or ", ".join(coverage_keys)
        label = prefix or ", ".join(coverage_keys)
        scope_note = (
            f"This campaign is scoped to areas under '{label}'. "
            f"Concentrate on: {hint}."
        )
        structural_extra = (
            f"{structural_extra}\n\n{scope_note}" if structural_extra else scope_note
        )
    # A whole-repo sweep needs the coverage contract MORE than an area part,
    # not less: without it the platform has no way to know what fraction of the
    # repository the run actually reached, and silence reads as full coverage.
    contract = coverage_contract(lens_keys=[lens.key])
    structural_extra = (
        f"{structural_extra}\n\n{contract}" if structural_extra else contract
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
    # Stamp AFTER dispatch: an escalation that never reached a sweep must not
    # be pushed down the queue.
    await _mark_escalations_delivered(escalated, run.uid)
    return run.uid


_SYNTHESIS_CONTRACT = """# What this run is

Every other part of this campaign has finished. Your job is NOT to audit the
repository again — it is to read what this campaign produced and say what it
MEANS, as one Analysis a maintainer can act on.

The campaign's own digest is a tally: counts by severity, coverage rows, a
list of parts. Nobody has read the findings AS A SET and answered the
questions a maintainer actually has — what is worst, what is systemic, what
should be done first, and where the audit itself fell short.

# Your inputs

- `list_findings` / `search_findings` — the findings this campaign filed.
  Read the real ones; do not work from the summary below alone.
- The campaign digest below: per-part coverage, per-lens verdicts, and the
  scopes no part completed.
- The repository itself, to check anything the findings leave ambiguous.

# What you produce

ONE Analysis, authored with the analysis tools:

* `upsert_analysis` — call early with a title and status="in_progress", then
  again at the end with `health_grade` (A-F), a `scorecard` over the
  dimensions this campaign actually covered, `confidence`, `limitations`,
  and `stats`. Finish with status="complete".
* `set_analysis_section` — at minimum `executive_summary` (what a maintainer
  needs to know in one paragraph), `top_changes` (the highest-value changes,
  ordered), and `implementation_plan` (staged: containment → low-risk fixes
  → reliability/security → structural work).
* `add_analysis_note` — note_type="coverage" per area, using the REAL
  coverage below rather than assuming full coverage; "strength" for what this
  campaign found to be in good shape; "validation" for checks that ran.

# Honesty requirements

- `limitations` must state what this campaign did NOT cover: scopes with no
  completed part, lenses skipped or never answered for, and — where a part
  reported no measured coverage — that its coverage is the scope it was
  aimed at rather than what it read.
- Do not invent severity. If the findings are all low, the grade says so.
- Do not file new findings. If you notice something new, it belongs to a
  later audit; this run reports on work already done.

Finish with `complete_run`."""


def _digest_block(summary: dict) -> str:
    """The campaign's own numbers, as the synthesis run's starting point."""
    coverage = dict(summary.get("coverage") or {})
    lines = ["## This campaign's digest"]
    counts = dict(summary.get("counts") or {})
    if counts:
        by_sev = ", ".join(
            f"{k}: {v}" for k, v in sorted((counts.get("by_severity") or {}).items())
        )
        lines.append(f"- findings: {counts.get('total', 0)}" + (f" ({by_sev})" if by_sev else ""))
    for row in coverage.get("lens_rollup") or []:
        lines.append(
            f"- lens {row.get('lens')}: {row.get('checked-clean', 0)} clean, "
            f"{row.get('checked-findings', 0)} with findings, "
            f"{row.get('skipped', 0)} skipped, {row.get('missing', 0)} no verdict"
        )
    for row in coverage.get("parts") or []:
        lines.append(
            f"- part {row.get('idx')} ({row.get('title')}): {row.get('state')}, "
            f"{row.get('covered', 0)} claimed / {row.get('observed', 0)} measured "
            f"[{row.get('coverage_source', 'unknown')}]"
        )
    holes = coverage.get("holes") or []
    if holes:
        lines.append(f"- scopes NO part completed: {', '.join(str(h) for h in holes[:20])}")
    return "\n".join(lines)


async def _dispatch_synthesis(campaign, part: dict) -> str:
    """Dispatch the campaign's synthesis run — one Analysis over its output.

    The digest is computed LIVE: this part dispatches while the campaign is
    still running, so `campaign.summary` has not been written yet, and a
    synthesis run reading an empty summary would report a campaign that found
    nothing.
    """
    from domains.campaigns.services.finalize import compute_digest

    summary, _findings = await compute_digest(campaign)
    structural = "\n\n".join([_SYNTHESIS_CONTRACT, _digest_block(summary)])
    composed = await compose_agent_intent(
        repository_uid=campaign.repository_uid,
        agent_key="deep-scan",  # the Analysis-authoring base
        prompt_body=None,
        structural=structural,
    )
    tier = normalize_effort(campaign.effort or "normal")
    policy = await ensure_policy_for_effort(tier)
    run = await trigger_run(
        repository_uid=campaign.repository_uid,
        intent=composed.text,
        playbook="ask",
        title=f"Synthesis: {campaign.title or 'campaign'}",
        target=_target(campaign, part, synthesis=True),
        run_policy_uid=policy.uid,
        effort=tier.value,
        agent_uid=composed.agent_uid,
        agent_rev=composed.agent_rev,
        trigger=_campaign_trigger(campaign),
        triggered_by=campaign.created_by or "campaign",
        composed_degraded=composed.composed_degraded,
        degraded_layers=composed.degraded_layers,
    )
    return run.uid


async def dispatch_part(campaign, part: dict) -> str:
    """Dispatch one pending part; returns the child run's uid."""
    kind = part.get("kind") or "area"
    if kind == "global":
        return await _dispatch_global(campaign, part)
    if kind == "feature":
        return await _dispatch_feature(campaign, part)
    if kind == "synthesis":
        return await _dispatch_synthesis(campaign, part)
    return await _dispatch_area(campaign, part)
