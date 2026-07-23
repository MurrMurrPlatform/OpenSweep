"""Two-stage sweep: Generate docs (page-tree proposals) then Audit (targeted).

The docs tree is the platform's only concept layer (KNOWLEDGE_V3). This
module holds the two repository-level entry points:

  run_generate_docs() - ONE LLM run that proposes the documentation page
                        tree via `propose_doc_edit` (new pages land as
                        pending DocEdits — the existing review surface).
                        No per-page fan out.

  run_audit()         - explicit, user-driven. User passes the doc uids
                        to audit (and optionally a custom intent to focus
                        the run). One Run per page, scoped to its
                        watch_paths via Run.target.

  run_map_areas()     - ONE LLM run that proposes the Area map (the audit
                        partition) via `propose_area_edit` (proposals land
                        as pending AreaEdits). Mirrors run_generate_docs.

Triggered Runs use the active LLM provider and the system-default
RunPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from domains.areas.models import Area, area_is_stale, is_leaf
from domains.docs.models import Doc
from domains.runs.schemas import RunTrigger
from domains.runs.services._intent_helpers import (
    build_intent,
    load_agent_prompt_body,
)
from domains.runs.services.lifecycle import (
    LifecycleError,
    trigger_run,
)
from infrastructure.audit import write_audit
from logging_config import logger


async def _workflow_prompt(repository_uid: str, stage: str) -> str | None:
    """Per-repo workflow default when the caller passed no explicit prompt."""
    from domains.repositories.services.workflow import stage_prompt_body

    return await stage_prompt_body(repository_uid, stage)


@dataclass
class GenerateDocsResult:
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class GenerateSpecsResult:
    repository_uid: str
    run_uid: str = ""
    # Feature-leaf keys the run was told to draft/refresh (no-spec or stale).
    targets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class MapAreasResult:
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ReviseSpecResult:
    run_uid: str = ""
    errors: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class AuditResult:
    repository_uid: str
    doc_count: int
    runs_dispatched: list[str] = field(default_factory=list)
    skipped_docs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: str = ""
    # Auto-selection provenance (§F): [{doc_uid, slug, reason}] — why each
    # page was picked. Empty for explicit doc_uids audits.
    selected: list[dict] = field(default_factory=list)


async def run_generate_docs(
    *,
    repository_uid: str,
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
) -> GenerateDocsResult:
    """Dispatch one generate-docs LLM run (KNOWLEDGE_V3_DOCUMENTATION §10).

    The LLM walks the repository itself (via its executor's native file
    tools) and proposes the documentation page tree via `propose_doc_edit`.
    Every page lands as a pending DocEdit for human review.

    Docs are scaffolded by the Area map (one page per subsystem area by
    default), so a repo with no enabled subsystem areas cannot generate:
    the gate raises LifecycleError BEFORE any dispatch — the API converts
    it to a 409. keep-docs-current / targeted doc updates are ungated.
    """
    subsystem_areas = [
        a
        for a in await Area.nodes.all()
        if a.repository_uid == repository_uid
        and bool(a.enabled)
        and (a.kind or "subsystem") == "subsystem"
    ]
    if not subsystem_areas:
        raise LifecycleError(
            "no area map — run Map areas first (docs are scaffolded by the "
            "area partition)"
        )

    result = GenerateDocsResult(repository_uid=repository_uid)

    try:
        existing_pages = await _existing_pages_listing(repository_uid)
        areas_listing = await _existing_areas_listing(repository_uid)
        prompt_body = await load_agent_prompt_body(agent_uid)
        if prompt_body is None:
            prompt_body = await _workflow_prompt(repository_uid, "discover")
        composed = await _generate_docs_intent(
            repository_uid=repository_uid,
            existing_pages_listing=existing_pages,
            areas_listing=areas_listing,
            prompt_body=prompt_body,
        )
        run = await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="ask",
            title="Generate documentation",
            stage="discover",
            agent_uid=composed.agent_uid,
            agent_rev=composed.agent_rev,
            composed_degraded=composed.composed_degraded,
            degraded_layers=composed.degraded_layers,
            trigger=RunTrigger.MANUAL,
            triggered_by=triggered_by or "generate-docs",
        )
        result.run_uid = run.uid
    except LifecycleError as exc:
        msg = f"generate-docs: {exc}"
        logger.warning(f"sweep: dispatch failed — {msg}")
        result.errors.append(msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"generate-docs: {type(exc).__name__}: {exc}"
        logger.warning(f"sweep: unexpected — {msg}")
        result.errors.append(msg)

    result.summary = (
        f"Generate docs: {'1 LLM run dispatched' if result.run_uid else 'no run dispatched'}"
    )

    await write_audit(
        kind="sweep.generate_docs_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "generate-docs",
        payload={
            "run_uid": result.run_uid,
            "errors": len(result.errors),
        },
    )

    return result


async def feature_leaf_spec_targets(repository_uid: str) -> list[Area]:
    """The enabled feature LEAVES that need a spec drafted or refreshed.

    A feature is a leaf when no other enabled feature key nests under it
    (leaf-ness computed over the feature key set only — parent feature
    groupings are charters, never audit/spec targets). A leaf is a target
    when it has NO spec, or its spec is STALE (code moved under it since the
    last review — the unified staleness axis). Sorted by key for a stable
    listing."""
    rows = [
        a
        for a in await Area.nodes.all()
        if a.repository_uid == repository_uid
        and bool(a.enabled)
        and a.kind == "feature"
    ]
    feature_keys = [a.key for a in rows]
    targets = [
        a
        for a in rows
        if is_leaf(a.key, feature_keys)
        and (not (a.spec or "").strip() or area_is_stale(a))
    ]
    targets.sort(key=lambda a: a.key)
    return targets


async def run_generate_specs(
    *,
    repository_uid: str,
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
) -> GenerateSpecsResult:
    """Dispatch one generate-specs LLM run (mirror of run_generate_docs).

    The LLM drafts specs for the feature LEAVES that lack one and refreshes
    the ones whose spec went stale, landing each as a pending AreaEdit
    (proposed_spec) via `propose_area_edit` for human accept — it never
    writes a spec directly.

    Gated on a feature map existing: a repo with no enabled feature areas
    that need a spec has nothing to generate, so the gate raises
    LifecycleError BEFORE any dispatch — the API converts it to a 409.
    """
    targets = await feature_leaf_spec_targets(repository_uid)
    if not targets:
        raise LifecycleError(
            "no feature leaves need a spec — map feature areas first, or every "
            "feature spec is already current"
        )

    result = GenerateSpecsResult(
        repository_uid=repository_uid, targets=[a.key for a in targets]
    )

    try:
        targets_listing = _feature_spec_targets_listing(targets)
        prompt_body = await load_agent_prompt_body(agent_uid)
        composed = await _generate_specs_intent(
            repository_uid=repository_uid,
            targets_listing=targets_listing,
        )
        run = await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="ask",
            title="Generate feature specs",
            stage="discover",
            agent_uid=composed.agent_uid,
            agent_rev=composed.agent_rev,
            composed_degraded=composed.composed_degraded,
            degraded_layers=composed.degraded_layers,
            trigger=RunTrigger.MANUAL,
            triggered_by=triggered_by or "generate-specs",
        )
        result.run_uid = run.uid
    except LifecycleError as exc:
        msg = f"generate-specs: {exc}"
        logger.warning(f"sweep: dispatch failed — {msg}")
        result.errors.append(msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"generate-specs: {type(exc).__name__}: {exc}"
        logger.warning(f"sweep: unexpected — {msg}")
        result.errors.append(msg)

    result.summary = (
        f"Generate specs: {'1 LLM run dispatched' if result.run_uid else 'no run dispatched'} "
        f"for {len(targets)} feature leaf/leaves"
    )

    await write_audit(
        kind="sweep.generate_specs_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "generate-specs",
        payload={
            "run_uid": result.run_uid,
            "targets": result.targets,
            "errors": len(result.errors),
        },
    )

    return result


async def revise_area_spec(
    *,
    repository_uid: str,
    area_uid: str,
    instruction: str,
    triggered_by: str = "",
) -> ReviseSpecResult:
    """Dispatch a one-shot LLM run that revises a single area's spec.

    Reads the area's current spec + scope paths, injects the maintainer's
    instruction, and proposes exactly ONE AreaEdit (proposed_spec) via
    `propose_area_edit` — mirroring `run_generate_specs` but scoped to a
    single area with a user instruction.

    Raises LifecycleError if the area does not exist.
    """
    area = await Area.nodes.get_or_none(uid=area_uid)
    if area is None:
        raise LifecycleError(f"area {area_uid} not found")

    result = ReviseSpecResult()

    try:
        paths = ", ".join(str(p) for p in (area.scope_paths or [])) or "(no scope paths)"
        current_spec = (area.spec or "").strip() or "(no spec yet)"
        existing_state_listing = (
            f"# Target area\n\n"
            f"key: {area.key}\n"
            f"title: {area.title or area.key}\n"
            f"scope_paths: {paths}\n\n"
            f"# Current spec\n\n"
            f"{current_spec}\n\n"
            f"# Maintainer instruction\n\n"
            f"{instruction}"
        )

        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=repository_uid,
            agent_key="revise-spec",
            structural=_GENERATE_SPECS_TOOLING_CONTRACT,
            existing_state_listing=existing_state_listing,
        )
        run = await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="ask",
            title=f"Revise spec — {area.key}",
            stage="discover",
            agent_uid=composed.agent_uid,
            agent_rev=composed.agent_rev,
            composed_degraded=composed.composed_degraded,
            degraded_layers=composed.degraded_layers,
            trigger=RunTrigger.MANUAL,
            triggered_by=triggered_by or "revise-spec",
        )
        result.run_uid = run.uid
    except LifecycleError as exc:
        msg = f"revise-spec: {exc}"
        logger.warning(f"sweep: dispatch failed — {msg}")
        result.errors.append(msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"revise-spec: {type(exc).__name__}: {exc}"
        logger.warning(f"sweep: unexpected — {msg}")
        result.errors.append(msg)

    result.summary = (
        f"Revise spec: {'1 LLM run dispatched' if result.run_uid else 'no run dispatched'} "
        f"for area {area.key}"
    )
    return result


async def map_areas_run_in_flight(repository_uid: str):
    """The active map-areas Run for this repository, or None (truthy = in
    flight). One guard shared by the API endpoint (409 with the run's
    detail) and the schedule scanner (skip the tick).

    Map-areas runs carry the map-areas system agent's uid as their agent
    provenance. When that agent was never seeded the guard is INERT —
    logged, then treated as nothing-in-flight (fail-open: a missing seed
    must not block mapping)."""
    from domains.agents.services.registry import system_agent_by_key
    from domains.runs.services.active_runs import active_runs_for

    map_agent = await system_agent_by_key("map-areas")
    if map_agent is None:
        logger.warning(
            f"map-areas in-flight guard inactive for {repository_uid}: "
            "system agent not seeded",
            extra={"tag": "areas"},
        )
        return None
    candidates = await active_runs_for(repository_uid=repository_uid)
    return next(
        (r for r in candidates if (r.agent_uid or "") == map_agent.uid), None
    )


async def run_map_areas(
    *,
    repository_uid: str,
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
    trigger: RunTrigger = RunTrigger.MANUAL,
) -> MapAreasResult:
    """Dispatch one map-areas LLM run.

    The LLM walks the repository itself (via its executor's native file
    tools) and proposes the Area map — the audit partition — via
    `propose_area_edit`. Every proposal lands as a pending AreaEdit for
    human review.
    """
    result = MapAreasResult(repository_uid=repository_uid)

    try:
        existing_areas = await _existing_areas_listing(repository_uid)
        docs_listing = await _docs_metadata_listing(repository_uid)
        # NO workflow-stage fallback here: the "discover" stage prompt is
        # generate-docs guidance ("build the documentation page tree via
        # propose_doc_edit") — composing it into a map run gives the agent
        # two conflicting task briefs, and it sometimes follows the docs
        # one (observed: a Map-areas run filing 25 DocEdits, 0 AreaEdits).
        # The map-areas base + tooling contract are the whole brief.
        prompt_body = await load_agent_prompt_body(agent_uid)
        composed = await _map_areas_intent(
            repository_uid=repository_uid,
            existing_areas_listing=existing_areas,
            docs_listing=docs_listing,
            prompt_body=prompt_body,
        )
        run = await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="ask",
            title="Map areas",
            stage="discover",
            agent_uid=composed.agent_uid,
            agent_rev=composed.agent_rev,
            composed_degraded=composed.composed_degraded,
            degraded_layers=composed.degraded_layers,
            trigger=trigger,
            triggered_by=triggered_by or "map-areas",
        )
        result.run_uid = run.uid
    except LifecycleError as exc:
        msg = f"map-areas: {exc}"
        logger.warning(f"sweep: dispatch failed — {msg}")
        result.errors.append(msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"map-areas: {type(exc).__name__}: {exc}"
        logger.warning(f"sweep: unexpected — {msg}")
        result.errors.append(msg)

    result.summary = (
        f"Map areas: {'1 LLM run dispatched' if result.run_uid else 'no run dispatched'}"
    )

    await write_audit(
        kind="sweep.map_areas_dispatched",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "map-areas",
        payload={
            "run_uid": result.run_uid,
            "errors": len(result.errors),
        },
    )

    return result


def _area_scope_block(areas: list[Area]) -> str:
    """The structural scope contract for an area-scoped ask run: paths per
    area, with feature specs inlined as the contract to verify (mirrors
    campaign feature parts) and subsystem specs as guidance."""
    lines = [
        "Scope this run to the following areas from the reviewed area map. "
        "Your scope is ONLY their paths — do not investigate outside it.",
    ]
    for a in areas:
        label = a.title or a.key
        lines.append("")
        lines.append(f"## {label} — `{a.key}` ({a.kind})")
        for p in a.scope_paths or []:
            lines.append(f"- {p}")
        spec = (a.spec or "").strip()
        if not spec:
            continue
        lines.append("")
        if a.kind == "feature":
            lines.append(
                "Feature spec — verify the implementation matches this "
                "contract end-to-end:"
            )
        else:
            lines.append("Area spec — what to check here:")
        lines.append(spec)
    return "\n".join(lines)


async def run_audit(
    *,
    repository_uid: str,
    doc_uids: list[str],
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
    custom_intent: Optional[str] = None,
    max_findings: Optional[int] = None,
    run_policy_uid: Optional[str] = None,
    effort: str = "",
    area_uids: Optional[list[str]] = None,
) -> AuditResult:
    """Dispatch one scoped audit Run per selected doc page.

    Focus lives in the intent text (custom_intent), not a taxonomy: pass
    "focus on security of the auth flows" instead of picking categories.
    max_findings is the numeric budget knob — it lands as an intent line
    (per run, so a 3-page audit with max_findings=5 caps each page at 5).
    area_uids (empty doc_uids only) narrows the repo-scoped run to the
    selected areas: their scope paths become the run's target and their
    specs ride along in the structural block.
    """
    if max_findings:
        budget = (
            f"File at most {max_findings} findings — rank by severity × confidence "
            "and file the clearest, highest-impact ones first. An empty result is "
            "a valid result."
        )
        custom_intent = f"{custom_intent}\n\n{budget}" if custom_intent else budget
    result = AuditResult(
        repository_uid=repository_uid,
        doc_count=len(doc_uids),
    )

    # No docs = whole-repository scope (V3 §8): ONE ask run dispatched
    # directly, no per-page fan out. This is the normal
    # path before the first Generate docs has populated the tree.
    if not doc_uids:
        # Optional area scoping: the selected areas' paths become the run's
        # target and their specs the structural contract.
        structural = "The whole repository (no doc-page scoping)."
        target: Optional[dict] = None
        title = "Repository audit"
        if area_uids:
            wanted_areas = set(area_uids)
            areas = sorted(
                (
                    a
                    for a in await Area.nodes.all()
                    if a.uid in wanted_areas
                    and a.repository_uid == repository_uid
                ),
                key=lambda a: a.key,
            )
            for missing in sorted(wanted_areas - {a.uid for a in areas}):
                result.errors.append(f"area={missing}: not found in repository")
            if not areas:
                result.summary = "Audit: no run dispatched (no matching areas)"
                return result
            structural = _area_scope_block(areas)
            target = {
                "area_keys": [a.key for a in areas],
                "paths": sorted({p for a in areas for p in a.scope_paths or []}),
            }
            keys = [a.key for a in areas]
            shown = ", ".join(keys[:3])
            title = f"Audit — {shown}" + (
                f" +{len(keys) - 3}" if len(keys) > 3 else ""
            )
        prompt_body = await load_agent_prompt_body(agent_uid)
        if prompt_body is None:
            prompt_body = await _workflow_prompt(repository_uid, "ask")
        # Org-agent-overlays composition: the chosen prompt (or the ask
        # stage's configured prompt) stays the instructions layer; the org
        # overlay applies on top; a custom_intent still wins outright.
        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=repository_uid,
            agent_key="ask",
            stage="ask",
            repo_guidance="",
            custom_intent=custom_intent,
            prompt_body=prompt_body,
            structural=structural,
        )
        intent = composed.text
        try:
            run = await trigger_run(
                repository_uid=repository_uid,
                intent=intent,
                playbook="ask",
                title=title,
                target=target,
                run_policy_uid=run_policy_uid,
                effort=effort,
                trigger=RunTrigger.MANUAL,
                triggered_by=triggered_by or "audit",
            )
            result.runs_dispatched.append(run.uid)
        except LifecycleError as exc:
            msg = f"repository audit: {exc}"
            logger.warning(f"sweep: dispatch failed — {msg}")
            result.errors.append(msg)
        result.summary = (
            "Audit: 1 repository-scoped run dispatched"
            if result.runs_dispatched
            else "Audit: no run dispatched"
        )
        await write_audit(
            kind="sweep.audit_completed",
            subject_uid=repository_uid,
            subject_type="Repository",
            actor_uid=triggered_by or "audit",
            payload={
                "doc_count": 0,
                "area_count": len(area_uids or []),
                "runs_dispatched": len(result.runs_dispatched),
                "errors": len(result.errors),
            },
        )
        return result

    wanted = set(doc_uids)
    docs: list[Doc] = [
        d for d in await Doc.nodes.all()
        if d.repository_uid == repository_uid and d.uid in wanted
    ]
    found = {d.uid for d in docs}
    for missing in wanted - found:
        result.skipped_docs.append(missing)
        result.errors.append(f"doc={missing}: not found in repository")

    prompt_body = await load_agent_prompt_body(agent_uid)
    if prompt_body is None:
        prompt_body = await _workflow_prompt(repository_uid, "ask")
    for doc in docs:
        try:
            run = await trigger_run(
                repository_uid=repository_uid,
                intent=_audit_intent(
                    doc=doc, prompt_body=prompt_body, custom_intent=custom_intent
                ),
                playbook="ask",
                title=f"Audit — {doc.title or doc.slug}",
                target={"doc_uids": [doc.uid], "paths": list(doc.watch_paths or [])},
                run_policy_uid=run_policy_uid,
                effort=effort,
                trigger=RunTrigger.MANUAL,
                triggered_by=triggered_by or "audit",
            )
            result.runs_dispatched.append(run.uid)
        except LifecycleError as exc:
            msg = f"doc={doc.uid}: {exc}"
            logger.warning(f"sweep: dispatch failed — {msg}")
            result.errors.append(msg)
            result.skipped_docs.append(doc.uid)
        except Exception as exc:  # noqa: BLE001
            msg = f"doc={doc.uid}: {type(exc).__name__}: {exc}"
            logger.warning(f"sweep: unexpected — {msg}")
            result.errors.append(msg)
            result.skipped_docs.append(doc.uid)

    result.summary = (
        f"Audit: {len(result.runs_dispatched)} run(s) dispatched "
        f"across {len(docs)} doc page(s)"
    )

    await write_audit(
        kind="sweep.audit_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "audit",
        payload={
            "doc_count": len(docs),
            "runs_dispatched": len(result.runs_dispatched),
            "errors": len(result.errors),
        },
    )

    return result


async def run_auto_audit(
    *,
    repository_uid: str,
    limit: int = 3,
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
    custom_intent: Optional[str] = None,
    max_findings: Optional[int] = None,
    run_policy_uid: Optional[str] = None,
    effort: str = "",
) -> AuditResult:
    """Staleness-driven audit (§F): auto-select the pages that most need a
    look (never checked, then longest-stale) and fan out through run_audit.
    Stale FEATURE leaves are re-audited too — one implementation-gaps audit
    scoped to them (run_audit's area path inlines each feature's spec as the
    contract to verify), so features are no longer only covered by `full`
    campaigns.

    Zero targets (no stale docs AND no stale feature leaves) returns an empty
    result WITHOUT dispatching — run_audit's empty-doc_uids path means "whole
    repository", which is never what a nothing-is-stale tick should do."""
    from domains.runs.services.audit_selection import select_audit_targets

    targets = await select_audit_targets(repository_uid, limit=limit)
    feature_leaves = await _select_stale_feature_leaves(repository_uid)
    if not targets and not feature_leaves:
        result = AuditResult(
            repository_uid=repository_uid,
            doc_count=0,
            summary="Auto-audit: nothing stale or unchecked",
        )
        await write_audit(
            kind="sweep.auto_audit_completed",
            subject_uid=repository_uid,
            subject_type="Repository",
            actor_uid=triggered_by or "auto-audit",
            payload={"selected": 0, "feature_leaves": 0},
        )
        return result

    result = AuditResult(repository_uid=repository_uid, doc_count=len(targets))
    if targets:
        result = await run_audit(
            repository_uid=repository_uid,
            doc_uids=[t.doc_uid for t in targets],
            triggered_by=triggered_by or "auto-audit",
            agent_uid=agent_uid,
            custom_intent=custom_intent,
            max_findings=max_findings,
            run_policy_uid=run_policy_uid,
            effort=effort,
        )
    # Stale feature leaves → one implementation-gaps audit scoped to them via
    # run_audit's area path (feature specs ride along as the contract). A
    # feature part in a campaign would do the same; here we dispatch directly.
    if feature_leaves:
        feature_result = await run_audit(
            repository_uid=repository_uid,
            doc_uids=[],
            area_uids=[a.uid for a in feature_leaves],
            triggered_by=triggered_by or "auto-audit",
            agent_uid=agent_uid,
            custom_intent=(
                "Audit these feature flows for implementation gaps: verify the "
                "code satisfies each feature's spec contract end-to-end."
            ),
            max_findings=max_findings,
            run_policy_uid=run_policy_uid,
            effort=effort,
        )
        result.runs_dispatched.extend(feature_result.runs_dispatched)
        result.errors.extend(feature_result.errors)

    result.selected = [
        {"doc_uid": t.doc_uid, "slug": t.slug, "reason": t.reason} for t in targets
    ] + [
        {"area_key": a.key, "slug": a.key, "reason": "stale-feature"}
        for a in feature_leaves
    ]
    result.summary = (
        f"Auto-audit: {len(targets)} stale page(s) + {len(feature_leaves)} "
        f"stale feature leaf/leaves, {len(result.runs_dispatched)} run(s) dispatched"
    )
    await write_audit(
        kind="sweep.auto_audit_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "auto-audit",
        payload={
            "selected": len(targets),
            "feature_leaves": len(feature_leaves),
            "reasons": {t.doc_uid: t.reason for t in targets},
            "runs_dispatched": len(result.runs_dispatched),
        },
    )
    return result


async def _select_stale_feature_leaves(repository_uid: str) -> list[Area]:
    """Enabled feature LEAVES whose spec is stale (code moved under them since
    last review) AND that have a spec to verify — the auto-audit re-audit
    targets. Feature leaves lacking a spec are the generate-specs flow's job,
    not an audit's (there is no contract to check yet). Excludes leaves an
    active run already covers (its target.area_keys), so a tick never
    double-dispatches the same feature."""
    from domains.runs.services.active_runs import active_runs_for

    rows = [
        a
        for a in await Area.nodes.all()
        if a.repository_uid == repository_uid
        and bool(a.enabled)
        and a.kind == "feature"
    ]
    feature_keys = [a.key for a in rows]
    in_flight_keys: set[str] = set()
    for run in await active_runs_for(repository_uid=repository_uid):
        for k in (dict(run.target or {}).get("area_keys") or []):
            in_flight_keys.add(str(k))
    leaves = [
        a
        for a in rows
        if is_leaf(a.key, feature_keys)
        and (a.spec or "").strip()
        and area_is_stale(a)
        and a.key not in in_flight_keys
    ]
    leaves.sort(key=lambda a: a.key)
    return leaves


@dataclass
class DeepScanResult:
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = field(default_factory=list)
    summary: str = ""


async def run_deep_scan(
    *,
    repository_uid: str,
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
    custom_intent: Optional[str] = None,
    max_findings: Optional[int] = None,
    run_policy_uid: Optional[str] = None,
    effort: str = "",
) -> DeepScanResult:
    """Dispatch ONE whole-repository deep-scan run (plan → sweep → synthesize).

    Unlike run_audit's per-page fan-out, this is a single long-running `ask`
    run whose intent tells the agent to survey the whole repo, plan its own
    scan, and work module by module — filing Findings as it goes and closing
    with one synthesis Finding. It runs on whatever executor the org's active
    provider selects (CLI providers get a git clone; internal_llm uses the
    read tools), gets analyzer candidates (ask playbook, §E), and should be
    dispatched under a `deep` effort policy for a generous wall ceiling.

    A single long-running run backs the scan; its agent provenance lets it be
    in-flight guarded and re-dispatched like the other sweep flows.
    """
    result = DeepScanResult(repository_uid=repository_uid)

    # Deep scans default to the `unlimited` policy — everything defaults to
    # unlimited for now; the analysis-stage pin or an explicit caller policy
    # still overrides. Precedence: an explicit caller policy wins; else the
    # analysis-stage pin (applied downstream at dispatch); else this default.
    # We only fill the default when neither of the higher-priority sources is
    # present, so the per-stage selector keeps its override power.
    if not run_policy_uid:
        from domains.runs.schemas import Effort
        from domains.repositories.services.workflow import stage_run_overrides
        from domains.run_policies.services.effort import ensure_policy_for_effort

        stage_pin = (
            await stage_run_overrides(repository_uid, "analysis")
        ).get("run_policy_uid") or ""
        if not stage_pin:
            deep_policy = await ensure_policy_for_effort(Effort.UNLIMITED)
            run_policy_uid = deep_policy.uid

    budget_line: Optional[str] = None
    if max_findings:
        budget_line = (
            f"File at most {max_findings} findings across the whole scan — rank by "
            "severity × confidence and file the clearest, highest-impact ones first."
        )

    try:
        # An explicit prompt override, or the repo's own "analysis" workflow
        # stage prompt, replaces the seeded deep-scan instructions. Both are
        # deliberate choices; the generic "ask" stage prompt is never pulled
        # here. There is no seeded default for the analysis stage, so by
        # default this stays None and the seeded "deep-scan" agent base (org
        # overlay applied) — the point of this flow — stands.
        prompt_body = await load_agent_prompt_body(agent_uid)
        if prompt_body is None:
            prompt_body = await _workflow_prompt(repository_uid, "analysis")
        composed = await _deep_scan_intent(
            repository_uid=repository_uid,
            prompt_body=prompt_body,
            focus=custom_intent,
            budget_line=budget_line,
        )
        run = await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="ask",
            title="Deep scan — whole repository",
            stage="analysis",
            agent_uid=composed.agent_uid,
            agent_rev=composed.agent_rev,
            composed_degraded=composed.composed_degraded,
            degraded_layers=composed.degraded_layers,
            run_policy_uid=run_policy_uid,
            effort=effort,
            trigger=RunTrigger.MANUAL,
            triggered_by=triggered_by or "deep-scan",
        )
        result.run_uid = run.uid
    except LifecycleError as exc:
        msg = f"deep-scan: {exc}"
        logger.warning(f"sweep: dispatch failed — {msg}")
        result.errors.append(msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"deep-scan: {type(exc).__name__}: {exc}"
        logger.warning(f"sweep: unexpected — {msg}")
        result.errors.append(msg)

    result.summary = (
        "Deep scan: 1 whole-repository run dispatched"
        if result.run_uid
        else "Deep scan: no run dispatched"
    )

    await write_audit(
        kind="sweep.deep_scan_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "deep-scan",
        payload={
            "run_uid": result.run_uid,
            "errors": len(result.errors),
        },
    )

    return result


async def _deep_scan_intent(
    *,
    repository_uid: str = "",
    prompt_body: Optional[str] = None,
    focus: Optional[str] = None,
    budget_line: Optional[str] = None,
):
    """Compose the deep-scan intent through the org-agent-overlays layers.

    The instructions layer is the seeded "deep-scan" agent base (org overlay
    applied on top; an explicit prompt override or analysis-stage pin still
    replaces it via prompt_body). Focus + budget + the Analysis authoring
    contract ride in the structural slot, AFTER every guidance layer, so no
    override or overlay can displace them — never routed through
    custom_intent, which would REPLACE the instructions outright.
    """
    from domains.agents.services.composition import compose_agent_intent

    scope_parts = ["The whole repository (no doc-page scoping)."]
    if focus and focus.strip():
        scope_parts.append(f"Focus for this scan: {focus.strip()}")
    if budget_line:
        scope_parts.append(budget_line)
    scope_parts.append(_DEEP_SCAN_ANALYSIS_CONTRACT)
    return await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="deep-scan",
        prompt_body=prompt_body,
        structural="\n\n".join(scope_parts),
    )


async def _existing_pages_listing(repository_uid: str) -> str:
    """Compact listing of existing Doc pages for the generate-docs prompt,
    so re-runs update pages instead of proposing duplicates."""
    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
    real = [d for d in docs if (d.body or "").strip()]
    if not real:
        return "(none yet — this is the first Generate docs for this repository)"
    lines: list[str] = []
    for d in sorted(real, key=lambda x: x.slug)[:120]:
        summary = (d.summary or "").strip().replace("\n", " ")
        if len(summary) > 120:
            summary = summary[:117] + "…"
        lines.append(f"- {d.slug}: {d.title or d.slug} :: {summary}")
    return "\n".join(lines)


async def _existing_areas_listing(repository_uid: str) -> str:
    """Compact listing of enabled Areas for the map-areas prompt, so re-runs
    update areas instead of proposing duplicates."""
    areas = [
        a for a in await Area.nodes.all()
        if a.repository_uid == repository_uid and a.enabled
    ]
    if not areas:
        return "(none yet — this is the first Map areas run)"
    lines: list[str] = []
    for a in sorted(areas, key=lambda x: x.key)[:150]:
        paths = ", ".join(str(p) for p in list(a.scope_paths or [])[:3])
        lines.append(
            f"- {a.key} [{a.kind or 'subsystem'}] {a.title or a.key} :: {paths}"
        )
    if len(areas) > 150:
        lines.append(f"… and {len(areas) - 150} more areas (elided)")
    return "\n".join(lines)


async def _docs_metadata_listing(repository_uid: str) -> str:
    """Metadata-only listing of the doc tree for the map-areas prompt: slug,
    uid, title, watch_paths — never bodies or summaries. Areas must be
    grounded in the code, not restate the docs; bodies are pulled
    deliberately via the `read_doc` tool when the agent decides it needs
    one. The uid is what an area proposal links back via doc_uids."""
    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
    if not docs:
        return "(no documentation pages yet)"
    lines: list[str] = []
    for d in sorted(docs, key=lambda x: x.slug)[:150]:
        watch = ", ".join(str(p) for p in (d.watch_paths or [])) or "(no watch paths)"
        lines.append(
            f"- {d.slug} (uid={d.uid}): {d.title or d.slug} :: watch_paths: {watch}"
        )
    if len(docs) > 150:
        lines.append(f"… and {len(docs) - 150} more pages (elided)")
    return "\n".join(lines)


async def _map_areas_intent(
    *,
    repository_uid: str,
    existing_areas_listing: str,
    docs_listing: str,
    prompt_body: Optional[str] = None,
):
    # The seeded "map-areas" agent base is the instructions layer (org
    # override applied; an explicit prompt or the repo's "discover" stage pin
    # replaces it via prompt_body). The propose_area_edit tooling contract
    # and the doc-tree metadata ride in the structural slot so no override
    # can displace them; the existing-areas listing lands in the
    # existing-state slot, exactly like generate-docs' pages listing.
    from domains.agents.services.composition import compose_agent_intent

    structural = (
        _MAP_AREAS_TOOLING_CONTRACT
        + "\n\n## Doc tree (metadata)\n"
        + docs_listing
    )
    return await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="map-areas",
        prompt_body=prompt_body,
        structural=structural,
        existing_state_listing=existing_areas_listing,
    )


async def _generate_docs_intent(
    *,
    repository_uid: str,
    existing_pages_listing: str,
    areas_listing: str = "",
    prompt_body: Optional[str] = None,
):
    # The seeded "generate-docs" agent base is the instructions layer (org
    # override applied; an explicit prompt or the repo's "discover" stage pin
    # replaces it via prompt_body). The propose_doc_edit tooling contract
    # and the Area-map metadata (the scaffold the page tree defaults to —
    # mirror of map-areas' docs listing) ride in the structural slot so no
    # override can displace them.
    from domains.agents.services.composition import compose_agent_intent

    structural = _GENERATE_DOCS_TOOLING_CONTRACT
    if areas_listing:
        structural += "\n\n## Area map (metadata)\n" + areas_listing
    return await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="generate-docs",
        prompt_body=prompt_body,
        structural=structural,
        existing_state_listing=existing_pages_listing,
    )


def _feature_spec_targets_listing(targets: list[Area]) -> str:
    """The feature leaves the generate-specs run must draft/refresh: key,
    title, scope paths, whether a spec exists, and whether it went stale."""
    lines: list[str] = []
    for a in targets:
        paths = ", ".join(str(p) for p in (a.scope_paths or [])) or "(no scope paths)"
        has_spec = bool((a.spec or "").strip())
        why = "stale — refresh" if has_spec else "no spec — draft"
        lines.append(f"- {a.key}: {a.title or a.key} :: scope: {paths} :: {why}")
    return "\n".join(lines)


async def _generate_specs_intent(
    *,
    repository_uid: str,
    targets_listing: str,
    prompt_body: Optional[str] = None,
):
    # The seeded "generate-specs" agent base is the instructions layer (org
    # override applied; an explicit prompt or the repo's "discover" stage pin
    # replaces it via prompt_body). The propose_area_edit tooling contract
    # (spec-author variant) rides in the structural slot so no override can
    # displace it; the target feature leaves land in the existing-state slot.
    from domains.agents.services.composition import compose_agent_intent

    return await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="generate-specs",
        prompt_body=prompt_body,
        structural=_GENERATE_SPECS_TOOLING_CONTRACT,
        existing_state_listing=targets_listing,
    )


def _audit_intent(
    *,
    doc: Doc,
    prompt_body: Optional[str] = None,
    custom_intent: Optional[str] = None,
) -> str:
    watch = ", ".join(doc.watch_paths or []) or "(no watch paths — use the page body to find the code)"
    default_intent = _AUDIT_INTENT_TEMPLATE.format(
        doc_title=doc.title or doc.slug,
        doc_slug=doc.slug,
        doc_uid=doc.uid,
        watch_paths=watch,
    )
    scope_summary = (
        f"doc_slug={doc.slug}\ntitle={doc.title or doc.slug}\n"
        f"watch_paths={watch}"
    )
    return build_intent(
        prompt_body=prompt_body,
        custom_intent=custom_intent,
        default_intent=default_intent,
        scope_summary=scope_summary,
    )


# Code-owned tooling contract for generate-docs runs — lands in the
# structural slot so an org overlay can never displace it. The task
# instructions themselves are the seeded "generate-docs" agent base
# (agents/services/seed_agent_bases.py). The existing-pages listing
# arrives via build_intent's existing-state slot.
_GENERATE_DOCS_TOOLING_CONTRACT = """# Tooling

Write tool — `propose_doc_edit(slug, title, summary, proposed_body,
watch_paths, rationale)`. Every call proposes ONE page (full body). New
slugs create pages; existing slugs replace their body."""


# Code-owned tooling contract for map-areas runs — lands in the structural
# slot so an org overlay can never displace it. The task instructions
# themselves are the seeded "map-areas" agent base
# (agents/services/seed_agent_bases.py). The existing-areas listing arrives
# via build_intent's existing-state slot; the doc-tree metadata is appended
# to this contract.
_MAP_AREAS_TOOLING_CONTRACT = """# Tooling

Write tool — `propose_area_edit(key, kind, title, scope_paths, spec,
doc_uids, rationale, enabled)`. Every call proposes ONE area (full
replacement on existing keys; `enabled=false` proposes retiring an area
that should no longer exist). kind is one of subsystem | feature |
ignore. The result's `warnings` list every partition conflict your
proposal creates — against the live map and your own earlier proposals
this run. Fix them by re-proposing; finish with zero warnings. Read a
doc page's body with `read_doc(slug)` only when you actually need it —
the listing below is metadata only."""


# Code-owned tooling contract for generate-specs runs — lands in the
# structural slot so an org overlay can never displace it. The task
# instructions are the seeded "generate-specs" agent base. The target
# feature leaves arrive via build_intent's existing-state slot.
_GENERATE_SPECS_TOOLING_CONTRACT = """# Tooling

Write tool — `propose_area_edit(key, kind, title, scope_paths, spec,
doc_uids, rationale, enabled)`. Call it once per feature leaf to propose
its spec: pass the leaf's exact `key`, `kind="feature"`, and the drafted
or refreshed contract as `spec`. Each call lands a pending AreaEdit
(proposed_spec) for human accept — you never write a spec directly. Leave
scope_paths/doc_uids as the leaf already has them unless the flow's real
entry points changed. Read the current code at each leaf's scope paths
with your native file tools before writing its contract."""


_AUDIT_INTENT_TEMPLATE = """Audit the code behind this documentation page.

Page: **{doc_title}** (slug={doc_slug})
Watch paths: {watch_paths}

Read the page with `read_doc(slug={doc_slug})`, then audit the code at
its watch paths. File high-signal Findings only:

* correctness/security defects -> kind=defect with severity reflecting impact.
* missing tests                -> kind=gap.
* stale/missing source-repo docs -> kind=gap, tags=["docs"].
* maintainability              -> kind=improvement.

Along the way, persist durable non-obvious facts with `write_memory`
(anchor_uid={doc_uid}), and if the page itself is wrong or stale, fix it
with `propose_doc_edit` — or `confirm_doc_current(slug={doc_slug})` if
you verified it and it holds.

Be concise. If the page's scope is small or unclear, file fewer Findings
rather than padding."""


# Code-owned Analysis authoring contract for deep-scan runs — lands in the
# structural slot so an org overlay can never displace it (a deep scan that
# forgets these tools produces no Analysis). The sweep guidance itself is the
# seeded "deep-scan" agent base (agents/services/seed_agent_bases.py).
_DEEP_SCAN_ANALYSIS_CONTRACT = """# How you record the Analysis (tools)

You are building ONE Analysis for this run. Author it incrementally so partial
progress is never lost:

* `upsert_analysis` — the verdict layer. Call early with a title and
  status="in_progress"; call again at the end with an overall `health_grade`
  (A-F), a `scorecard` (list of {dimension, score, max, grade, rationale} over
  correctness, security, performance, reliability, data_integrity,
  maintainability, testing, documentation, architecture, dependencies,
  observability, dev_experience), `confidence` (confirmed|high|medium|low),
  `limitations`, and `stats`. Finish with status="complete".
* `set_analysis_section` — one narrative section per call. Author, at least:
  executive_summary, repository_map, security_summary, performance_summary,
  dependency_report, test_gap_report, implementation_plan (staged: Stage 0
  containment → 1 low-risk fixes → 2 reliability/security → 3 simplification →
  4 structural refactor), and top_changes (the ~10 highest-value changes).
  Bodies are markdown; reference finding evidence inline.
* `add_analysis_note` — append auditable rows: note_type="coverage" for each
  area you examined (status examined|partial|skipped — your running checklist
  so nothing is silently missed), "strength" for well-designed areas (report
  BOTH defects and what is good), and "validation" for each check you ran or
  couldn't (build, tests, lint, type-check…).
* `create_finding` — one per concrete issue (the actionable atoms). These
  join to this Analysis automatically.
* `ask_question` — for anything you cannot resolve from the code alone and
  need a human to answer (production data, runtime metrics, product intent,
  deployment context). Explain why it matters. These are answerable and can
  drive a refined re-scan.

# Finish

Set the verdict (`upsert_analysis` with health_grade + scorecard + confidence +
limitations + stats, status="complete"), make sure executive_summary,
implementation_plan, and top_changes sections are written, then call
`complete_run` with a short outcome summary."""


def estimate_sweep_cost(num_docs: int) -> dict[str, Any]:
    """Cheap shape — frontend hint, not a hard number."""
    return {
        "docs": num_docs,
        "generate_docs_runs": 1,
        "audit_runs_if_all_selected": num_docs,
        "note": (
            "Generate docs dispatches one LLM run that only proposes the "
            "documentation page tree. Audit is explicit: user picks which "
            "pages (optionally with a custom focus intent); one LLM run is "
            "dispatched per selected page."
        ),
    }
