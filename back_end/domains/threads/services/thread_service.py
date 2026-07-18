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


async def resolve_thread(candidate: str, *, run_uid: str = "") -> Thread | None:
    """Self-healing thread resolution for platform tools (agents mix up the
    thread/ticket uids — both opaque hex). Order:
      1. exact Thread uid,
      2. the calling run's own thread (run.thread_uid — most reliable),
      3. the ACTIVE thread of a ticket uid.
    Returns None only when nothing matches."""
    candidate = (candidate or "").strip()
    if candidate:
        t = await Thread.nodes.get_or_none(uid=candidate)
        if t is not None:
            return t
    if run_uid:
        from domains.investigations.models import Run

        run = await Run.nodes.get_or_none(uid=run_uid)
        if run is not None and (run.thread_uid or ""):
            t = await Thread.nodes.get_or_none(uid=run.thread_uid)
            if t is not None:
                return t
    if candidate:
        threads = await Thread.nodes.filter(subject_ticket_uid=candidate)
        for t in threads:
            if t.phase not in TERMINAL_PHASES:
                return t
    return None


THREAD_NOT_FOUND_DETAIL = (
    "thread not found — pass the Thread uid from your instructions (the "
    "ticket uid also works while its thread is active)"
)


def open_question_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "question" and e.get("status") == "open"]


def pending_answer_events(events: list[dict]) -> list[dict]:
    """Answered questions whose answers were not yet delivered to the agent —
    batch gating (multiple questions per turn) accumulates them here."""
    return [
        e
        for e in events
        if e.get("type") == "question"
        and e.get("status") == "answered"
        and not e.get("delivered_at")
    ]


def build_answers_message(answered: list[dict], skipped: list[dict] | None = None) -> str:
    """One combined message delivering every accumulated answer (and, on a
    forced continue, naming what the user chose not to answer)."""
    lines = ["Answers to your questions:"]
    for e in answered:
        lines.append(f'- Q: "{e.get("question", "")}"\n  A: {e.get("answer", "")}')
    if skipped:
        lines.append(
            "\nThe user chose to CONTINUE WITHOUT ANSWERING these — proceed "
            "with your best judgment and say what you assumed:"
        )
        for e in skipped:
            lines.append(f'- Q: "{e.get("question", "")}"')
    return "\n".join(lines)


async def route_comment_reply(*, parent_comment_uid: str, body: str, actor_uid: str) -> None:
    """A human reply under a thread-question mirror comment IS the answer:
    resolve the question and resume the conversation. Called best-effort from
    the comment-create route — never raises."""
    try:
        from domains.comments.models import Comment

        parent = await Comment.nodes.get_or_none(uid=parent_comment_uid)
        if parent is None:
            return
        meta = dict(parent.meta or {})
        if meta.get("kind") != "thread_question" or meta.get("status") == "answered":
            return
        thread_uid = str(meta.get("thread_uid") or "")
        question_uid = str(meta.get("question_uid") or "")
        if not thread_uid or not question_uid:
            return
        await ThreadService().answer_question(
            thread_uid,
            question_uid,
            body,
            actor_uid=actor_uid,
            mirror_comment=False,  # the reply itself is the visible answer
            deliver=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"comment-reply routing failed for {parent_comment_uid}: {exc}",
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
            **base,
            plan_text=t.plan_text or "",
            todos=dict(t.todos or {}),
            events=t.events or [],
            runs=runs,
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
        self,
        uid: str,
        question_uid: str,
        answer: str,
        *,
        actor_uid: str,
        mirror_comment: bool = True,
        deliver: bool = True,
    ) -> Thread:
        """Answer a structured `question` from EITHER surface (thread UI or a
        ticket-comment reply): marks the event answered, syncs the mirror
        comment's meta, posts the answer as a reply under the mirror
        (unless the answer arrived AS that reply), and delivers it into the
        conversation so the agent resumes."""
        t = await self.get_node(uid)
        if not (answer or "").strip():
            raise HTTPException(status_code=422, detail="answer must be non-empty")
        now = datetime.now(UTC)
        events = list(t.events or [])
        question_event: dict | None = None
        for event in events:
            if event.get("type") == "question" and event.get("uid") == question_uid:
                if event.get("status") == "answered":
                    return t  # idempotent
                event["status"] = "answered"
                event["answer"] = answer.strip()
                event["answered_by"] = actor_uid
                event["answered_at"] = now.isoformat()
                question_event = event
                break
        else:
            raise HTTPException(status_code=404, detail="question not found")
        t.events = events
        t.updated_at = now
        await t.save()

        question_text = str(question_event.get("question") or "")
        mirror_uid = str(question_event.get("comment_uid") or "")

        # Sync the mirror comment's meta (chips flip to answered). Best-effort.
        if mirror_uid:
            try:
                from domains.comments.models import Comment

                mirror = await Comment.nodes.get_or_none(uid=mirror_uid)
                if mirror is not None:
                    meta = dict(mirror.meta or {})
                    meta["status"] = "answered"
                    mirror.meta = meta
                    await mirror.save()
            except Exception:  # noqa: BLE001
                pass

        # Post the answer as a REPLY under the mirrored question — skipped
        # when the answer itself arrived as that reply. Best-effort.
        if mirror_comment:
            try:
                from domains.comments import service as comment_service
                from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

                body = f"✅ {answer.strip()}"
                if not mirror_uid and question_text:
                    short = (
                        question_text
                        if len(question_text) <= 160
                        else question_text[:160] + "…"
                    )
                    body = f"✅ **Answer** to “{short}”:\n\n{answer.strip()}"
                await comment_service.create_comment(
                    subject_type=CommentSubjectType.TICKET,
                    subject_uid=t.subject_ticket_uid,
                    body=body,
                    author_uid=actor_uid,
                    author_kind=CommentAuthorKind.USER,
                    parent_comment_uid=mirror_uid,
                )
            except Exception:  # noqa: BLE001
                pass

        # Batch gating: with several questions in flight the agent resumes
        # only once ALL are answered (or the user forces continue).
        if deliver:
            await self._deliver_pending_answers(t)
        return t

    async def _deliver_pending_answers(self, t: Thread, *, force: bool = False) -> bool:
        """Deliver accumulated answers as ONE message when no questions remain
        open (or on force: deliver what's answered, dismiss the rest).
        Returns True when a message was sent."""
        if not t.active_run_uid:
            return False
        events = list(t.events or [])
        open_qs = open_question_events(events)
        pending = pending_answer_events(events)
        if open_qs and not force:
            return False  # still waiting on answers — keep accumulating
        if not pending and not (force and open_qs):
            return False  # nothing to deliver
        now = datetime.now(UTC)
        for e in pending:
            e["delivered_at"] = now.isoformat()
        if force:
            for e in open_qs:
                e["status"] = "dismissed"
                await self._sync_mirror_status(e, "dismissed")
        t.events = events
        t.updated_at = now
        await t.save()

        from domains.threads.services.intents import PLANNING_TURN_REMINDER
        from domains.threads.services.thread_run import send_message_turn

        text = build_answers_message(pending, skipped=open_qs if force else None)
        if t.phase == "refining":
            text = f"{text}\n\n{PLANNING_TURN_REMINDER}"
        send_message_turn(t.active_run_uid, text)
        return True

    async def _sync_mirror_status(self, question_event: dict, status: str) -> None:
        """Best-effort: keep the mirrored ticket comment's meta in step."""
        mirror_uid = str(question_event.get("comment_uid") or "")
        if not mirror_uid:
            return
        try:
            from domains.comments.models import Comment

            mirror = await Comment.nodes.get_or_none(uid=mirror_uid)
            if mirror is not None:
                meta = dict(mirror.meta or {})
                meta["status"] = status
                mirror.meta = meta
                await mirror.save()
        except Exception:  # noqa: BLE001
            pass

    async def continue_without_answers(self, uid: str, *, actor_uid: str) -> Thread:
        """User forces the conversation on: deliver whatever is answered,
        dismiss the open questions."""
        t = await self.get_node(uid)
        delivered = await self._deliver_pending_answers(t, force=True)
        if not delivered:
            raise HTTPException(status_code=409, detail="no pending questions to continue past")
        await self.record_event(t, "questions_continued", by=actor_uid)
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
