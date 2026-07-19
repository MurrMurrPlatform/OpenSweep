"""Refine-run dispatch — one read-only run that enriches a ticket in place.

Shared by the ticket refine route and the PR-import flow (a ticket created
for an externally-opened PR can be AI-drafted from the PR's actual diff).
"""

from __future__ import annotations

from fastapi import HTTPException


def build_ticket_refine_intent(t) -> str:
    ac = "\n".join(f"- {c}" for c in (t.acceptance_criteria or [])) or "- (none yet)"
    return (
        "Refine the Ticket below: study the relevant code, then sharpen the "
        "ticket in place using the platform tools. This is read-only against "
        "the repository — do not modify any code.\n"
        "\n"
        f"Ticket uid: {t.uid}\n"
        f"Title: {t.title}\n"
        f"Priority: {t.priority}\n"
        "\n"
        "Current description:\n"
        f"{(t.description or '(not provided)').strip()}\n"
        "\n"
        "Current acceptance criteria:\n"
        f"{ac}\n"
        "\n"
        "Task:\n"
        "1. Read the code the ticket touches to ground the work in reality. "
        "Quote concrete file:line references.\n"
        "2. Call `opensweep_platform_update_ticket` (ticket_uid "
        f"`{t.uid}`) to improve `title`, rewrite `description` so it is "
        "implementable without re-deriving the analysis, and set "
        "`acceptance_criteria` to 2-6 short, independently testable clauses.\n"
        "3. Attach an implementation plan and the list of relevant files with "
        f"`opensweep_platform_attach_artifact` (target_type `ticket`, target_uid "
        f"`{t.uid}`, artifact_type `plan`) — the concrete steps and files a "
        "developer should touch.\n"
        "Persist every conclusion through the tools above — a plan in your reply "
        "that is not written back does not count. Do not change the ticket's "
        "status; Gate 1 stays human-only."
    )


async def dispatch_refine_run(
    ticket, *, actor_uid: str, org_uid: str, extra_context: str = ""
):
    """Trigger the read-only refine run for a ticket. Raises HTTPException 409
    when the lifecycle refuses the dispatch."""
    from domains.agents.services.composition import compose_agent_intent
    from domains.runs.schemas import Effort, RunTrigger
    from domains.runs.services.lifecycle import LifecycleError, trigger_run
    from domains.repositories.services.workflow import stage_prompt_body
    from domains.run_policies.services.effort import ensure_policy_for_effort

    structural = build_ticket_refine_intent(ticket)
    if extra_context:
        structural = f"{structural}\n\n{extra_context}"
    guidance = await stage_prompt_body(ticket.repository_uid, "refine")
    composed = await compose_agent_intent(
        repository_uid=ticket.repository_uid,
        agent_key="refine",
        stage="refine",
        repo_guidance=guidance or "",
        structural=structural,
        org_uid=org_uid,
    )
    policy = await ensure_policy_for_effort(Effort.NORMAL)
    try:
        return await trigger_run(
            repository_uid=ticket.repository_uid,
            intent=composed.text,
            playbook="refine",
            title=f"Refine: {(ticket.title or 'ticket')[:80]}",
            target={"ticket_uid": ticket.uid},
            linked_ticket_uid=ticket.uid,
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=actor_uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
