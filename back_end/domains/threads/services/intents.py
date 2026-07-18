"""First-turn prompts for thread sessions + implement carry-over addendum.

The session prompt reuses the read-only `refine` playbook but turns it into
plan mode: interrogate the user through the conversation, ground the ticket
in code, then persist the plan via the platform tools. Prompt-building is
pure so it stays testable without a DB (mirror: api/v1/tickets.py
_build_ticket_refine_intent).
"""

from __future__ import annotations

# Appended to platform-delivered answers while the thread is refining — the
# staged contract must ride with every turn (frontend keeps an identical copy
# for user-typed messages).
PLANNING_TURN_REMINDER = (
    "[Thread protocol reminder — PLANNING stage: do not edit files or commit; "
    "the platform will send an explicit GO message when implementation is "
    "approved. For now: ask the next question via opensweep_platform_ask_user, "
    "or update the ticket and submit the plan via "
    "opensweep_platform_submit_thread_plan, then stop and wait.]"
)


def build_thread_session_intent(ticket, thread_uid: str) -> str:
    ac = "\n".join(f"- {c}" for c in (ticket.acceptance_criteria or [])) or "- (none yet)"
    return (
        "You are the agent for a Thread: ONE continuous conversation that "
        "carries the Ticket below from refinement through planning to "
        "implementation and review fixes. You will do ALL of it, here, with "
        "full memory — but each stage starts only when the platform says so.\n"
        "\n"
        "## CURRENT STAGE: PLANNING (until the platform's GO message)\n"
        "- Your workspace already has the work branch checked out, but "
        "NOTHING leaves it: the platform pushes only after the user "
        "approves implementation. Until the platform sends you an explicit "
        "message beginning with 'GO —', do not edit files and do not "
        "commit — planning turns with code changes are discarded.\n"
        "- In this stage your deliverables are tool calls only: "
        "`opensweep_platform_ask_user` (clarifying questions), "
        "`opensweep_platform_update_ticket` (sharpened ticket), and "
        "`opensweep_platform_submit_thread_plan` (the implementation plan). "
        "A plan that exists only in your reply text does not count.\n"
        "- This holds for EVERY turn until the GO message arrives, "
        "including after the user answers a question: continue the "
        "protocol — next question, or ticket update + plan — then STOP "
        "and wait. The GO message, when it comes, carries the "
        "implementation rules.\n"
        "- `opensweep_platform_add_comment` is DISABLED for this "
        "conversation (calls are rejected). Questions go through "
        "`ask_user`; the plan goes through `submit_thread_plan` (the ONLY "
        "place the platform reads it from); everything else is said right "
        "here in the conversation. The platform mirrors your questions to "
        "the ticket's discussion automatically.\n"
        "\n"
        f"Ticket uid: {ticket.uid}\n"
        f"Thread uid: {thread_uid}\n"
        f"Title: {ticket.title}\n"
        f"Priority: {ticket.priority}\n"
        "\n"
        "Current description:\n"
        f"{(ticket.description or '(not provided)').strip()}\n"
        "\n"
        "Current acceptance criteria:\n"
        f"{ac}\n"
        "\n"
        "Task:\n"
        "1. Study the code the ticket touches. Quote concrete file:line "
        "references.\n"
        "2. Interrogate the user: ask ALL currently independent clarifying "
        "questions in ONE turn — one `opensweep_platform_ask_user` call per "
        f"question (thread_uid `{thread_uid}`, question, optional `options` "
        "list of 2-6 short choices) — then end your turn and wait. The "
        "platform holds the conversation until EVERY question is answered "
        "(or the user forces continue) and delivers all answers together; "
        "ask follow-ups that depend on earlier answers in a later round. "
        "Questions are also mirrored to the ticket's discussion, where the "
        "user can answer by replying. Surface trade-offs and your "
        "recommendation inside each question. Do NOT silently assume "
        "answers to open product questions.\n"
        f"3. Call `opensweep_platform_update_ticket` (ticket_uid `{ticket.uid}`) "
        "to sharpen title/description and set 2-6 independently testable "
        "acceptance criteria, reflecting what you learned.\n"
        "4. When the user is satisfied, write the implementation plan with "
        f"`opensweep_platform_submit_thread_plan` (thread_uid `{thread_uid}`, "
        "plan_markdown). The plan must cover the concrete approach, the "
        "files to touch, subagent strategy if any, and a 'How to verify' "
        "section. Keep iterating on the plan in this conversation until the "
        "user approves or tells you to stop.\n"
        "5. After submitting the plan: summarize it in one short paragraph, "
        "then STOP and wait. Implementation starts when the platform sends "
        "the GO message into this conversation — never before.\n"
        "Persist conclusions through the tools — a plan only in your reply "
        "does not count. Do not change the ticket's status; Gate 1 stays "
        "human-only."
    )


def build_group_addendum(children: list) -> str:
    """Group flow: the parent ticket is implemented as ONE unit — one branch,
    one PR that closes every subticket. List the members so the agent covers
    them all."""
    if not children:
        return ""
    lines = []
    for c in children:
        ac = "; ".join(str(a) for a in (c.acceptance_criteria or [])[:6])
        desc = (c.description or "").strip().replace("\n", " ")
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append(
            f"- `{c.uid}` {c.title}\n"
            f"  {desc or '(no description)'}\n"
            f"  Acceptance: {ac or '(none recorded)'}"
        )
    return (
        "\n\n# Ticket group — implement ALL subtickets in this one branch/PR\n"
        "This ticket is a group parent. The batch below ships as one unit; the\n"
        "PR that closes the parent closes every subticket. Cover each one and\n"
        "say per subticket in your summary what you did (or why you skipped it).\n\n"
        + "\n".join(lines)
    )


def build_implement_addendum(plan_text: str, decision_log: str) -> str:
    plan = (plan_text or "").strip()
    log = (decision_log or "").strip()
    if not plan and not log:
        return ""
    parts = ["\n\n# Context carried over from the planning thread\n"]
    if plan:
        parts.append(
            "Follow this plan; deviate only when the code contradicts it, and "
            "say so in your summary when you do.\n\n" + plan
        )
    if log:
        parts.append("\n\n" + log)
    return "".join(parts)
