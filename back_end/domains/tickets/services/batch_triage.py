"""Triage N tickets in ONE run — the question-deduplication path.

Per-ticket refine costs one run and one interrogation per ticket, so thirty
tickets cost thirty of each. The questions repeat: thirty tickets typically
raise a handful of distinct POLICY questions and a long tail of local ones. A
batch run reads the same code once, classifies every question across the whole
selection, answers the local ones itself, and spends the human's attention only
on what genuinely applies to several tickets.

Pure intent builder + thin async dispatcher, mirroring `refine_dispatch`.
"""

from __future__ import annotations

from domains.runs.schemas import Effort

#: Above this, context grows faster than it earns: a 60-ticket batch is a
#: failed run with a large bill. `EpicSelection.limit` defaults to 20, so this
#: only bites deliberate over-asking.
MAX_BATCH = 25

#: Per-candidate description budget in the prompt — same clamp the grouping
#: intent uses, for the same reason.
_DESC_CHARS = 200


def effort_for_batch(n: int) -> Effort:
    """One run over `n` tickets needs the ceiling of `n` runs, not of one.

    In code rather than on the request so a caller cannot ask for 25 tickets on
    a SHORT policy — the ceiling is a property of the work, not a preference.
    """
    return Effort.NORMAL if n <= 10 else Effort.DEEP


def _candidate_block(candidates) -> str:
    lines = []
    for f in candidates:
        desc = (f.description or "").strip().replace("\n", " ")
        if len(desc) > _DESC_CHARS:
            desc = desc[:_DESC_CHARS] + "…"
        paths = ", ".join(f.paths[:3]) if f.paths else "none recorded"
        lines.append(
            f"- `{f.uid}` [{f.priority or 'medium'}] {f.title}\n"
            f"  {desc or '(no description)'}\n"
            f"  paths: {paths}"
        )
    return "\n".join(lines)


def _policy_block(memories) -> str:
    """Policies already decided, seeded as prior art.

    Same framing `suggest_epics` uses for computed clusters: showing the agent
    what already exists is what stops it billing a human for an answer the
    platform already has.
    """
    rows = [m for m in memories or [] if (getattr(m, "title", "") or "").strip()]
    if not rows:
        return (
            "\nNo policies have been recorded for this repository yet — the "
            "first batch is expected to raise several.\n"
        )
    out = ["\nPolicies already decided for this repository. These are ANSWERED:"]
    for m in rows:
        body = (getattr(m, "body", "") or "").strip().replace("\n", " ")
        if len(body) > 300:
            body = body[:300] + "…"
        out.append(f"- {m.title}: {body}")
    out.append(
        "Do NOT ask any of these again. Apply them, and say which one you "
        "applied when it decides a question.\n"
    )
    return "\n".join(out)


def build_batch_triage_intent(candidates, policies, *, repository_uid: str) -> str:
    """The triage contract. Pure."""
    return (
        "You are triaging a BATCH of tickets in one pass. Your job is to make "
        "every one of them implementable without a human having to answer the "
        "same question thirty times.\n"
        "\n"
        f"Repository: {repository_uid}\n"
        f"Tickets in this batch ({len(candidates)}):\n"
        f"{_candidate_block(candidates)}\n"
        f"{_policy_block(policies)}"
        "\n"
        "Method — follow the ORDER, it is the whole point:\n"
        "1. Read the code each ticket touches. Quote concrete file:line "
        "references.\n"
        "2. List every open question across ALL tickets BEFORE asking any of "
        "them. Asking as you go produces the same question at ticket 3 and "
        "again, differently worded, at ticket 17 — which is the exact waste "
        "this batch exists to remove.\n"
        "3. Classify each question:\n"
        "   POLICY — the answer changes how you would write TWO OR MORE of "
        "these tickets. Conventions, error-handling stance, API shape, "
        "'do we support X at all'.\n"
        "   LOCAL — the answer changes exactly one ticket.\n"
        "4. LOCAL questions: answer them YOURSELF. Pick the most conventional "
        "answer for this codebase, cite the file:line precedent you followed, "
        "and record it with `opensweep_platform_record_assumption`.\n"
        "5. POLICY questions: check the policy list above first. Only if "
        "genuinely unanswered, call "
        "`opensweep_platform_ask_policy_question` and name EVERY ticket it "
        "changes in `applies_to_ticket_uids` (at least two — that list is "
        "what makes it a policy rather than a label you chose).\n"
        "6. Then call `opensweep_platform_update_ticket` for EVERY ticket: "
        "sharpen title/description and set 2-6 independently testable "
        "acceptance criteria.\n"
        "\n"
        "Tickets that already carry 2+ acceptance criteria and a recorded "
        "assumption were triaged in an earlier batch — verify them cheaply, "
        "do not redo them.\n"
        "\n"
        "If you run short of budget, `complete_run.skipped` MUST list the uids "
        "you did not reach, one per entry. Everything you did write is already "
        "saved; the list is what makes the rest resumable."
    )


async def dispatch_batch_triage(
    *, repository_uid: str, candidates, actor_uid: str, org_uid: str, autonomy: str
):
    """Compose and dispatch the batch triage run. 409 on lifecycle refusal."""
    from fastapi import HTTPException

    from domains.agents.services.composition import compose_agent_intent
    from domains.executors.prompt_kit import autonomy_block
    from domains.memory.services import memory_service
    from domains.repositories.services.workflow import stage_prompt_body
    from domains.run_policies.services.effort import ensure_policy_for_effort
    from domains.runs.schemas import RunTrigger
    from domains.runs.services.lifecycle import LifecycleError, trigger_run

    try:
        policies = await memory_service.search_memory(
            repository_uid=repository_uid, query="Policy:", limit=20
        )
    except Exception:  # noqa: BLE001 — seeding is an optimisation, not a gate
        policies = []

    effort = effort_for_batch(len(candidates))
    guidance = await stage_prompt_body(repository_uid, "refine")
    composed = await compose_agent_intent(
        repository_uid=repository_uid,
        agent_key="refine",
        stage="refine",
        repo_guidance=guidance or "",
        # The triage template IS the instructions for this run, so it goes in
        # custom_intent where a `replace` overlay cannot displace it — the same
        # call and the same reason as the grouping run.
        custom_intent=build_batch_triage_intent(
            candidates, policies, repository_uid=repository_uid
        ),
        # The question policy is code-owned and lands in the undisplaceable
        # scope slot.
        structural=autonomy_block(
            autonomy,
            ask_tool="opensweep_platform_ask_policy_question",
            subject_ref="this run",
        ),
        org_uid=org_uid,
    )
    policy = await ensure_policy_for_effort(effort)
    try:
        return await trigger_run(
            repository_uid=repository_uid,
            intent=composed.text,
            playbook="refine",
            title=f"Triage {len(candidates)} tickets",
            target={
                "kind": "ticket-triage",
                "ticket_uids": [c.uid for c in candidates],
            },
            run_policy_uid=policy.uid,
            effort=effort.value,
            autonomy=autonomy,
            trigger=RunTrigger.MANUAL,
            triggered_by=actor_uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
