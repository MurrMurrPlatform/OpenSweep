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

Triggered Runs use the active LLM provider and the system-default
RunPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
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
    """
    result = GenerateDocsResult(repository_uid=repository_uid)

    try:
        existing_pages = await _existing_pages_listing(repository_uid)
        prompt_body = await load_agent_prompt_body(agent_uid)
        if prompt_body is None:
            prompt_body = await _workflow_prompt(repository_uid, "discover")
        composed = await _generate_docs_intent(
            repository_uid=repository_uid,
            existing_pages_listing=existing_pages,
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


async def run_audit(
    *,
    repository_uid: str,
    doc_uids: list[str],
    triggered_by: str = "",
    agent_uid: Optional[str] = None,
    custom_intent: Optional[str] = None,
    max_findings: Optional[int] = None,
    run_policy_uid: Optional[str] = None,
) -> AuditResult:
    """Dispatch one scoped audit Run per selected doc page.

    Focus lives in the intent text (custom_intent), not a taxonomy: pass
    "focus on security of the auth flows" instead of picking categories.
    max_findings is the numeric budget knob — it lands as an intent line
    (per run, so a 3-page audit with max_findings=5 caps each page at 5).
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
        prompt_body = await load_agent_prompt_body(agent_prompt_uid)
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
            structural="The whole repository (no doc-page scoping).",
        )
        intent = composed.text
        try:
            run = await trigger_run(
                repository_uid=repository_uid,
                intent=intent,
                playbook="ask",
                title="Repository audit",
                run_policy_uid=run_policy_uid,
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
) -> AuditResult:
    """Staleness-driven audit (§F): auto-select the pages that most need a
    look (never checked, then longest-stale) and fan out through run_audit.

    Zero targets returns an empty result WITHOUT dispatching — run_audit's
    empty-doc_uids path means "whole repository", which is never what a
    nothing-is-stale tick should do."""
    from domains.runs.services.audit_selection import select_audit_targets

    targets = await select_audit_targets(repository_uid, limit=limit)
    if not targets:
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
            payload={"selected": 0},
        )
        return result

    result = await run_audit(
        repository_uid=repository_uid,
        doc_uids=[t.doc_uid for t in targets],
        triggered_by=triggered_by or "auto-audit",
        agent_uid=agent_uid,
        custom_intent=custom_intent,
        max_findings=max_findings,
        run_policy_uid=run_policy_uid,
    )
    result.selected = [
        {"doc_uid": t.doc_uid, "slug": t.slug, "reason": t.reason} for t in targets
    ]
    result.summary = f"Auto-audit: {result.summary} ({len(targets)} auto-selected)"
    await write_audit(
        kind="sweep.auto_audit_completed",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=triggered_by or "auto-audit",
        payload={
            "selected": len(targets),
            "reasons": {t.doc_uid: t.reason for t in targets},
            "runs_dispatched": len(result.runs_dispatched),
        },
    )
    return result


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

    # Deep scans default to the `deep` effort policy (a generous ceiling), but
    # the repo can override it by pinning a policy on the `analysis` workflow
    # stage. Precedence: an explicit caller policy wins; else the analysis-stage
    # pin (applied downstream at dispatch); else this deep default. We only fill
    # the default when neither of the higher-priority sources is present, so the
    # per-stage selector keeps its override power.
    if not run_policy_uid:
        from domains.runs.schemas import Effort
        from domains.repositories.services.workflow import stage_run_overrides
        from domains.run_policies.services.effort import ensure_policy_for_effort

        stage_pin = (
            await stage_run_overrides(repository_uid, "analysis")
        ).get("run_policy_uid") or ""
        if not stage_pin:
            deep_policy = await ensure_policy_for_effort(Effort.DEEP)
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
            run_policy_uid=run_policy_uid,
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


async def _generate_docs_intent(
    *,
    repository_uid: str,
    existing_pages_listing: str,
    prompt_body: Optional[str] = None,
):
    # The seeded "generate-docs" agent base is the instructions layer (org
    # override applied; an explicit prompt or the repo's "discover" stage pin
    # replaces it via prompt_body). The propose_doc_edit tooling contract
    # rides in the structural slot so no override can displace it.
    from domains.agents.services.composition import compose_agent_intent

    return await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="generate-docs",
        prompt_body=prompt_body,
        structural=_GENERATE_DOCS_TOOLING_CONTRACT,
        existing_state_listing=existing_pages_listing,
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
# (agent_prompts/services/seed_agent_bases.py). The existing-pages listing
# arrives via build_intent's existing-state slot.
_GENERATE_DOCS_TOOLING_CONTRACT = """# Tooling

Write tool — `propose_doc_edit(slug, title, summary, proposed_body,
watch_paths, rationale)`. Every call proposes ONE page (full body). New
slugs create pages; existing slugs replace their body."""


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
# seeded "deep-scan" agent base (agent_prompts/services/seed_agent_bases.py).
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
