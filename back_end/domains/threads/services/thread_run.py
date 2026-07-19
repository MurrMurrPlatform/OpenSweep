"""The `thread` playbook — ONE conversation, phase-gated by the platform
(unified dev flow rev2).

A thread run lives in a write sandbox (work branch created up front) for the
whole ticket lifecycle. The agent never pushes; the platform's write gate
decides what leaves the sandbox — so the phase gate is enforced HERE, not in
the prompt:

- refining      → finalize is a no-op (nothing the agent does goes anywhere)
- implementing / in_review
                → each completed turn reuses the implement finalize:
                  validate → push → adopt-or-open draft PR. Conversational
                  turns (no commits) stay quiet.

"Implement" is a platform-authored go-message into the SAME conversation
(send_message_turn), never a new run.
"""

from __future__ import annotations

import asyncio

from logging_config import logger

# Keep strong references to fire-and-forget turn tasks (asyncio only holds
# weak ones — a GC'd task silently dies mid-turn).
_TURN_TASKS: set[asyncio.Task] = set()


async def run_accepts_message(run_uid: str) -> bool:
    """Would a message turn start now? Status-based pre-check so platform-
    authored messages are never fired at a mid-turn run (they would 409 in
    the background and be silently lost)."""
    from fastapi import HTTPException

    from domains.investigations.models import Run
    from domains.investigations.services.turn_service import ensure_can_send

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return False
    try:
        ensure_can_send(run.status or "", False, playbook=run.playbook or "")
    except HTTPException:
        return False
    return True


async def finalize_thread_run(run) -> None:
    """Per-turn playbook hook. Never raises (playbooks.py contract)."""
    from domains.threads.models import Thread

    thread_uid = (getattr(run, "thread_uid", "") or "").strip()
    if not thread_uid:
        return
    thread = await Thread.nodes.get_or_none(uid=thread_uid)
    if thread is None:
        return
    # Turn boundary = retry point for answer batches that were held while the
    # run was mid-turn (delivery is guarded, never lost). Best-effort.
    try:
        from domains.threads.services.thread_service import ThreadService

        await ThreadService()._deliver_pending_answers(thread)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"pending-answer retry failed for thread {thread_uid}: {exc}",
            extra={"tag": "threads"},
        )
    if thread.phase in {"refining", "done", "abandoned"}:
        # Planning turns produce plan/ticket state via platform tools only;
        # the sandbox contents are inert until the phase gate opens.
        return
    from domains.delivery.services.implement_run_service import finalize_implement_run

    try:
        await finalize_implement_run(run, quiet_when_unchanged=True)
    except Exception as exc:  # noqa: BLE001
        # A failed push / PR open is invisible from inside the conversation —
        # without this the thread just sits in `implementing` with changed
        # files and no PR and no explanation (seen live: a push rejected for
        # missing repo write permission). Surface it on the thread timeline.
        detail = f"{type(exc).__name__}: {exc}"[:500]
        logger.warning(
            f"thread {thread_uid}: delivery finalize failed: {detail}",
            extra={"tag": "threads"},
        )
        try:
            from domains.threads.services.thread_service import ThreadService

            last = (thread.events or [])[-1] if thread.events else {}
            if not (last.get("type") == "delivery_blocked" and last.get("detail") == detail):
                await ThreadService().record_event(thread, "delivery_blocked", detail=detail)
        except Exception:  # noqa: BLE001
            pass  # timeline bookkeeping must not break the turn

    # Ready signal follow-through: un-draft the PR + auto-dispatch review
    # (workflow booleans permitting). After the delivery finalize so it sees
    # the just-pushed head and the freshly opened/adopted PR; runs on every
    # turn — its own guards make it a no-op until the thread is flagged.
    from domains.threads.services.hooks import maybe_ready_and_review_for_thread

    await maybe_ready_and_review_for_thread(thread_uid)


def send_message_turn(run_uid: str, text: str) -> None:
    """Deliver a platform-authored message into a run's conversation as a
    background turn (same pattern as chat first-turn dispatch in
    api/v1/runs.py). Live watchers stream it over the existing WS tailer."""

    async def _consume() -> None:
        from domains.investigations.services.turn_service import TurnService

        try:
            async for _ in TurnService().run_turn(run_uid, text):
                pass
        except Exception as exc:  # noqa: BLE001 — surfaced via run status/events
            logger.warning(
                f"thread message turn failed for run {run_uid}: {type(exc).__name__}: {exc}",
                extra={"tag": "threads"},
            )

    task = asyncio.create_task(_consume())
    _TURN_TASKS.add(task)
    task.add_done_callback(_TURN_TASKS.discard)


def build_go_message(
    *,
    ticket,
    plan_state: str,
    plan_text: str,
    work_branch: str,
    base_branch: str,
    denylist: list[str],
    children: list | None = None,
) -> str:
    """The platform's implementation go-signal, sent into the thread
    conversation when the user approves. Carries the write-run contract that
    one-shot implement runs get in their intent."""
    deny_block = "\n".join(f"  - `{p}`" for p in denylist) or "  - (none configured)"
    if plan_state in {"approved", "drafted"} and (plan_text or "").strip():
        plan_block = (
            f"Follow the plan you submitted (state: {plan_state}); deviate only "
            "when the code contradicts it, and say so in your summary when you do.\n\n"
            f"{plan_text.strip()}"
        )
    else:
        plan_block = (
            "No plan was drafted — implement the ticket as refined in this "
            "conversation, minimally."
        )
    group_block = ""
    if children:
        from domains.threads.services.intents import build_group_addendum

        group_block = build_group_addendum(children)
    return (
        "GO — the user approved implementation. PLANNING MODE IS OVER for "
        "this conversation: implement now, in this workspace.\n"
        "\n"
        f"{plan_block}\n"
        "\n"
        "## Working copy\n"
        f"- The work branch `{work_branch}` is already checked out in your "
        "current directory. Base branch: `{base}`. Never switch branches.\n".replace(
            "{base}", base_branch
        )
        + "\n"
        "## Rules (the platform validates your commits after every turn)\n"
        "- Make the MINIMAL change that satisfies the acceptance criteria — "
        "no drive-by refactors.\n"
        "- Do NOT touch any path matching these forbidden patterns:\n"
        f"{deny_block}\n"
        f"- Commit with conventional commit message(s) referencing "
        f"`OpenSweep-Ticket: {ticket.uid}`.\n"
        "- DO NOT push. Never run `git push` — the platform validates and "
        "pushes your branch after each turn and opens the draft PR.\n"
        "- Run the repository's test suites where feasible and make them "
        "pass; report failures honestly.\n"
        "- Attach a TEST NOTE via `attach_artifact` (target_type `ticket`, "
        f"target_uid `{ticket.uid}`, artifact_type `test_note`): concrete "
        "manual verification steps for a human on this branch.\n"
        "- When done, summarize: commits made (sha + message), test results, "
        "and anything you deviated on — then call "
        "`opensweep_platform_submit_for_review` (thread_uid "
        f"`{ticket.uid}`) to signal the work is ready. The platform then "
        "marks the PR ready and dispatches the independent review; its "
        "findings will arrive in this conversation.\n"
        f"{group_block}"
    )


def build_fix_message(pr, findings: list[dict], *, fix_round: int, max_rounds: int) -> str:
    """Review findings delivered into the thread conversation (replaces a
    separate cold fix run for thread-owned PRs)."""
    lines = []
    for f in findings[:20]:
        title = str(f.get("title") or "(untitled finding)")
        detail = str(f.get("why_it_matters") or "").strip().replace("\n", " ")
        fix_hint = str(f.get("suggested_fix") or "").strip().replace("\n", " ")
        if len(detail) > 200:
            detail = detail[:200] + "…"
        if len(fix_hint) > 200:
            fix_hint = fix_hint[:200] + "…"
        paths = ", ".join((f.get("affected_paths") or [])[:4])
        blocking = " [BLOCKING]" if f.get("blocking") else ""
        line = f"- **{title}**{blocking}{f' ({paths})' if paths else ''} — resolution `{f.get('resolution_uid', '')}`"
        if detail:
            line += f"\n  Why: {detail}"
        if fix_hint:
            line += f"\n  Suggested: {fix_hint}"
        lines.append(line)
    listing = "\n".join(lines) or "- (see the PR's findings in OpenSweep)"
    return (
        f"REVIEW FEEDBACK — fix round {fix_round}/{max_rounds} for "
        f"PR #{pr.github_number}.\n"
        "The independent review found blocking issues on your branch. Fix "
        "them in this workspace, commit, and record each resolution with "
        "`opensweep_platform_attach_fix`. Same rules as before: minimal "
        "changes, never push, the platform pushes after your turn and the "
        "review re-runs automatically.\n"
        "\n"
        f"{listing}"
    )
