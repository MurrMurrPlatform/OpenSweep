"""First-turn prompts for thread sessions + implement carry-over addendum.

The session prompt reuses the read-only `refine` playbook but turns it into
plan mode: interrogate the user through the conversation, ground the ticket
in code, then persist the plan via the platform tools. Prompt-building is
pure so it stays testable without a DB (mirror: api/v1/tickets.py
_build_ticket_refine_intent).
"""

from __future__ import annotations


def build_thread_session_intent(ticket, thread_uid: str) -> str:
    ac = "\n".join(f"- {c}" for c in (ticket.acceptance_criteria or [])) or "- (none yet)"
    return (
        "You are opening a planning conversation (a Thread) for the Ticket "
        "below. This is read-only against the repository — do not modify any "
        "code. Your job, in order:\n"
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
        "2. Interrogate the user: ask clarifying questions one question at a "
        "time, in plain language, and wait for each answer before asking the "
        "next. Surface trade-offs and your recommendation. Do NOT silently "
        "assume answers to open product questions.\n"
        f"3. Call `opensweep_platform_update_ticket` (ticket_uid `{ticket.uid}`) "
        "to sharpen title/description and set 2-6 independently testable "
        "acceptance criteria, reflecting what you learned.\n"
        "4. When the user is satisfied, write the implementation plan with "
        f"`opensweep_platform_submit_thread_plan` (thread_uid `{thread_uid}`, "
        "plan_markdown). The plan must list concrete steps, the files to "
        "touch, and a 'How to verify' section. Keep iterating on the plan in "
        "this conversation until the user approves or tells you to stop.\n"
        "Persist conclusions through the tools — a plan only in your reply "
        "does not count. Do not change the ticket's status; Gate 1 stays "
        "human-only."
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
