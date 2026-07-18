"""ThreadService — orchestrates the refine→plan→implement conversation.

Threads reference Runs, never replace them. Phase moves only through
`transition` (matrix-checked + audited). One active (non-terminal) thread
per ticket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.threads.models import Thread, is_legal_phase_transition
from domains.threads.schemas import ThreadDetailDTO, ThreadDTO, ThreadRunSummaryDTO
from domains.threads.services.intents import build_thread_session_intent
from infrastructure.audit import write_audit
from logging_config import logger

TERMINAL_PHASES = {"done", "abandoned"}


def thread_to_dto(t) -> ThreadDTO:
    return ThreadDTO(
        uid=t.uid,
        repository_uid=t.repository_uid,
        subject_ticket_uid=t.subject_ticket_uid,
        phase=t.phase,
        plan_state=t.plan_state,
        branch=t.branch or "",
        pr_uid=t.pr_uid or "",
        active_run_uid=t.active_run_uid or "",
        created_by=t.created_by or "",
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def has_active_thread(threads: list) -> bool:
    return any(t.phase not in TERMINAL_PHASES for t in threads)


async def mirror_plan_to_ticket(thread: Thread) -> None:
    """The plan's canonical public home is the TICKET's `plan` JSON metadata
    (user-facing, survives the thread). The thread stays the editing surface;
    every plan write flows through here. Best-effort — a mirror failure never
    breaks the plan write itself."""
    try:
        from domains.tickets.models import Ticket

        ticket = await Ticket.nodes.get_or_none(uid=thread.subject_ticket_uid)
        if ticket is None:
            return
        now = datetime.now(UTC)
        ticket.plan = {
            "markdown": thread.plan_text or "",
            "state": thread.plan_state or "none",
            "thread_uid": thread.uid,
            "updated_at": now.isoformat(),
            "approved_by": thread.plan_approved_by or "",
            "approved_at": thread.plan_approved_at.isoformat()
            if thread.plan_approved_at
            else None,
        }
        ticket.updated_at = now
        await ticket.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"plan mirror to ticket failed for thread {thread.uid}: {exc}",
            extra={"tag": "threads"},
        )


def compose_addendum_for_thread(plan_state: str, plan_text: str, events: list[dict]) -> str:
    from domains.threads.services.decision_log import build_decision_log
    from domains.threads.services.intents import build_implement_addendum

    plan = plan_text if plan_state in {"approved", "drafted"} else ""
    return build_implement_addendum(plan, build_decision_log(events))


class ThreadService:
    async def get_node(self, uid: str) -> Thread:
        t = await Thread.nodes.get_or_none(uid=uid)
        if t is None:
            raise HTTPException(status_code=404, detail="not found")
        return t

    async def list(
        self, *, repository_uid: str = "", subject_ticket_uid: str = ""
    ) -> list[Thread]:
        qs = Thread.nodes
        if repository_uid:
            qs = qs.filter(repository_uid=repository_uid)
        if subject_ticket_uid:
            qs = qs.filter(subject_ticket_uid=subject_ticket_uid)
        return list(await qs.all())

    async def record_event(self, thread: Thread, type: str, **payload) -> None:
        now = datetime.now(UTC)
        thread.events = [
            *(thread.events or []),
            {"ts": now.isoformat(), "type": type, **payload},
        ]
        thread.updated_at = now
        await thread.save()

    async def attach_run(self, thread: Thread, run_uid: str) -> None:
        from domains.investigations.models import Run

        if run_uid in (thread.run_uids or []):
            # Idempotent: rev2 threads keep ONE run for their whole life —
            # re-attachment (fix messaging etc.) must not duplicate timeline.
            thread.active_run_uid = run_uid
            await thread.save()
            return
        thread.run_uids = [*(thread.run_uids or []), run_uid]
        thread.active_run_uid = run_uid
        await thread.save()
        run = await Run.nodes.get_or_none(uid=run_uid)
        if run is not None:
            run.thread_uid = thread.uid
            await run.save()
        await self.record_event(thread, "run_attached", run_uid=run_uid)

    async def create(self, *, ticket_uid: str, actor_uid: str, org_uid: str) -> Thread:
        """One run, one conversation (rev2): the thread run starts in a WRITE
        sandbox on the ticket's work branch. Harmless while refining — the
        agent never pushes; the phase-gated finalizer holds the write gate
        shut until the user approves implementation."""
        # Imports local to avoid cycles, mirroring api/v1/tickets.py.
        from domains.delivery.services.implement_run_service import branch_name_for_ticket
        from domains.delivery.services.run_dispatch import require_repository
        from domains.execution.services.sandbox_service import SandboxService
        from domains.investigations.schemas import (
            ExecutionMode,
            Executor,
            InvestigationEffort,
            RunTrigger,
        )
        from domains.investigations.services.lifecycle import LifecycleError, trigger_run
        from domains.repositories.services.repository_service import repository_to_dto
        from domains.run_policies.services.effort import ensure_policy_for_effort
        from domains.tickets.services.ticket_service import TicketService
        from infrastructure.git_providers import get_provider_client

        ticket = await TicketService().get_node(ticket_uid)
        existing = await self.list(subject_ticket_uid=ticket_uid)
        if has_active_thread(existing):
            raise HTTPException(status_code=409, detail="ticket already has an active thread")
        repo = await require_repository(ticket.repository_uid, require_github=True)

        work_branch = branch_name_for_ticket(ticket)
        base_branch = repo.default_branch or "main"
        # Adopt an existing remote branch (earlier thread/implement attempt).
        checkout_existing = False
        client = get_provider_client(repo)
        if client.is_active:
            branch = await client.get_branch(repo.github_owner, repo.github_repo, work_branch)
            checkout_existing = branch is not None

        thread = Thread(
            uid=uuid4().hex,
            repository_uid=ticket.repository_uid,
            subject_ticket_uid=ticket_uid,
            branch=work_branch,
            created_by=actor_uid,
        )
        await thread.save()

        repo_dto = repository_to_dto(repo)

        async def _make_sandbox():
            return await SandboxService().create_for_write(
                repository=repo_dto,
                agent_run_uid=thread.uid,
                work_branch=work_branch,
                base_branch=base_branch,
                checkout_existing=checkout_existing,
            )

        policy = await ensure_policy_for_effort(InvestigationEffort.NORMAL)
        try:
            run = await trigger_run(
                repository_uid=ticket.repository_uid,
                intent=build_thread_session_intent(ticket, thread.uid),
                playbook="thread",
                title=f"Thread: {(ticket.title or 'ticket')[:80]}",
                target={
                    "thread_uid": thread.uid,
                    "ticket_uid": ticket_uid,
                    "work_branch": work_branch,
                    "base_branch": base_branch,
                },
                linked_ticket_uid=ticket_uid,
                executor=Executor.CLAUDE_CODE,
                execution_mode=ExecutionMode.IMPLEMENT,
                run_policy_uid=policy.uid,
                trigger=RunTrigger.MANUAL,
                triggered_by=actor_uid,
                sandbox_factory=_make_sandbox,
            )
        except LifecycleError as exc:
            await thread.delete()  # dispatch never started — no orphan threads
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await self.attach_run(thread, run.uid)

        # Work has genuinely started: a todo ticket moves to in-progress on
        # the board. Backlog tickets stay put — Gate 1 remains human-only.
        if (ticket.status or "") == "todo":
            try:
                await TicketService().transition(
                    ticket.uid, "in-progress", actor_uid=actor_uid, actor_role="maintainer"
                )
            except HTTPException:
                pass  # board bookkeeping must never fail thread creation

        await write_audit(
            kind="thread.created",
            subject_uid=thread.uid,
            subject_type="Thread",
            actor_uid=actor_uid,
            payload={"ticket_uid": ticket_uid, "run_uid": run.uid, "work_branch": work_branch},
        )
        return thread

    async def get_detail(self, uid: str) -> ThreadDetailDTO:
        from domains.investigations.models import Run

        t = await self.get_node(uid)
        runs: list[ThreadRunSummaryDTO] = []
        for run_uid in t.run_uids or []:
            r = await Run.nodes.get_or_none(uid=run_uid)
            if r is not None:
                runs.append(
                    ThreadRunSummaryDTO(
                        uid=r.uid,
                        playbook=r.playbook,
                        status=r.status,
                        title=r.title or "",
                        created_at=r.created_at,
                    )
                )
        base = thread_to_dto(t).model_dump()
        return ThreadDetailDTO(
            **base, plan_text=t.plan_text or "", events=t.events or [], runs=runs
        )

    async def update_plan(self, uid: str, plan_text: str, *, actor_uid: str) -> Thread:
        t = await self.get_node(uid)
        if t.phase in TERMINAL_PHASES:
            raise HTTPException(status_code=409, detail=f"thread is {t.phase}")
        t.plan_text = plan_text
        t.plan_state = "drafted"  # hand-edits invalidate approval
        await t.save()
        await mirror_plan_to_ticket(t)
        await self.record_event(t, "plan_edited", by=actor_uid)
        await write_audit(
            kind="thread.plan_edited",
            subject_uid=uid,
            subject_type="Thread",
            actor_uid=actor_uid,
            payload={},
        )
        return t

    async def approve_plan(self, uid: str, *, actor_uid: str) -> Thread:
        t = await self.get_node(uid)
        if not (t.plan_text or "").strip():
            raise HTTPException(status_code=409, detail="no plan to approve")
        now = datetime.now(UTC)
        t.plan_state = "approved"
        t.plan_approved_by = actor_uid
        t.plan_approved_at = now
        await t.save()
        await mirror_plan_to_ticket(t)
        await self.record_event(t, "plan_approved", by=actor_uid)
        await write_audit(
            kind="thread.plan_approved",
            subject_uid=uid,
            subject_type="Thread",
            actor_uid=actor_uid,
            payload={},
        )
        return t

    async def answer_question(
        self, uid: str, question_uid: str, answer: str, *, actor_uid: str
    ) -> Thread:
        """Mark a structured `question` event answered. The answer itself is
        delivered to the agent as a normal follow-up message by the caller
        (the thread chat) — this records the metadata side."""
        t = await self.get_node(uid)
        if not (answer or "").strip():
            raise HTTPException(status_code=422, detail="answer must be non-empty")
        now = datetime.now(UTC)
        events = list(t.events or [])
        for event in events:
            if event.get("type") == "question" and event.get("uid") == question_uid:
                if event.get("status") == "answered":
                    return t  # idempotent
                event["status"] = "answered"
                event["answer"] = answer.strip()
                event["answered_by"] = actor_uid
                event["answered_at"] = now.isoformat()
                break
        else:
            raise HTTPException(status_code=404, detail="question not found")
        t.events = events
        t.updated_at = now
        await t.save()

        # Mirror the answer next to the mirrored question on the ticket's
        # discussion. Best-effort.
        try:
            from domains.comments import service as comment_service
            from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

            question_text = next(
                (
                    str(e.get("question") or "")
                    for e in events
                    if e.get("type") == "question" and e.get("uid") == question_uid
                ),
                "",
            )
            body = f"✅ **Answer**: {answer.strip()}"
            if question_text:
                short = question_text if len(question_text) <= 160 else question_text[:160] + "…"
                body = f"✅ **Answer** to “{short}”:\n\n{answer.strip()}"
            await comment_service.create_comment(
                subject_type=CommentSubjectType.TICKET,
                subject_uid=t.subject_ticket_uid,
                body=body,
                author_uid=actor_uid,
                author_kind=CommentAuthorKind.USER,
            )
        except Exception:  # noqa: BLE001 — mirroring must never fail the answer
            pass
        return t

    async def transition(self, uid: str, to_phase: str, *, actor_uid: str) -> Thread:
        t = await self.get_node(uid)
        if not is_legal_phase_transition(t.phase, to_phase):
            raise HTTPException(
                status_code=409, detail=f"illegal transition {t.phase} → {to_phase}"
            )
        frm = t.phase
        t.phase = to_phase
        await t.save()
        await self.record_event(t, "phase_changed", frm=frm, to=to_phase, by=actor_uid)
        await write_audit(
            kind="thread.phase_changed",
            subject_uid=uid,
            subject_type="Thread",
            actor_uid=actor_uid,
            payload={"from": frm, "to": to_phase},
        )
        return t

    async def start_implement(self, uid: str, *, actor_uid: str):
        """Rev2: implementation is a platform-authored GO message into the
        SAME conversation — the phase flip opens the write gate; no new run.
        Legacy threads (separate-run v1) fall back to the old dispatch."""
        from domains.investigations.models import Run
        from domains.tickets.services.ticket_service import TicketService

        t = await self.get_node(uid)
        if t.phase != "refining":
            raise HTTPException(status_code=409, detail=f"thread is {t.phase}, not refining")
        ticket = await TicketService().get_node(t.subject_ticket_uid)

        run = (
            await Run.nodes.get_or_none(uid=t.active_run_uid) if t.active_run_uid else None
        )
        if run is None or run.playbook != "thread":
            return await self._start_implement_legacy(t, ticket, actor_uid=actor_uid)

        # Gate 1 stays human-only and stays enforced: no go-signal for a
        # ticket that was never approved onto the board.
        if (ticket.status or "") not in {"todo", "in-progress"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    "ticket must have passed Gate 1 (status todo or in-progress) — "
                    f"it is '{ticket.status}'"
                ),
            )
        if ticket.status == "todo":
            await TicketService().transition(
                ticket.uid, "in-progress", actor_uid=actor_uid, actor_role="maintainer"
            )

        from domains.delivery.services.resolution_service import ensure_merge_policy
        from domains.delivery.services.write_gate import effective_denylist
        from domains.threads.services.thread_run import build_go_message, send_message_turn
        from domains.tickets.models import Ticket

        target = dict(run.target or {})
        policy = await ensure_merge_policy(t.repository_uid)
        children = list(await Ticket.nodes.filter(parent_ticket_uid=ticket.uid))
        go = build_go_message(
            ticket=ticket,
            plan_state=t.plan_state,
            plan_text=t.plan_text or "",
            work_branch=str(target.get("work_branch") or t.branch or ""),
            base_branch=str(target.get("base_branch") or "main"),
            denylist=effective_denylist(policy),
            children=children,
        )
        await self.transition(uid, "implementing", actor_uid=actor_uid)
        await self.record_event(t, "implement_started", by=actor_uid)
        send_message_turn(run.uid, go)
        return run

    async def _start_implement_legacy(self, t: Thread, ticket, *, actor_uid: str):
        """v1 threads (refine-playbook conversation): dispatch a separate
        implement run with decision-log carry-over, as originally shipped."""
        from domains.delivery.services.implement_run_service import trigger_implement_run
        from domains.investigations.services.run_events import read_events
        from domains.threads.services.intents import build_group_addendum
        from domains.tickets.models import Ticket

        events: list[dict] = []
        if t.active_run_uid:
            try:
                events = read_events(t.active_run_uid)
            except Exception:  # noqa: BLE001 — carry-over is best-effort
                logger.warning(
                    f"thread {t.uid}: could not read session events for carry-over",
                    extra={"tag": "threads"},
                )
        addendum = compose_addendum_for_thread(t.plan_state, t.plan_text or "", events)
        children = list(await Ticket.nodes.filter(parent_ticket_uid=ticket.uid))
        addendum += build_group_addendum(children)

        run = await trigger_implement_run(
            ticket, triggered_by=actor_uid, intent_addendum=addendum
        )
        await self.attach_run(t, run.uid)
        await self.transition(t.uid, "implementing", actor_uid=actor_uid)
        return run
