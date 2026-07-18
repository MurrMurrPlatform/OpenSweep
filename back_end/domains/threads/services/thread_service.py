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

        thread.run_uids = [*(thread.run_uids or []), run_uid]
        thread.active_run_uid = run_uid
        await thread.save()
        run = await Run.nodes.get_or_none(uid=run_uid)
        if run is not None:
            run.thread_uid = thread.uid
            await run.save()
        await self.record_event(thread, "run_attached", run_uid=run_uid)

    async def create(self, *, ticket_uid: str, actor_uid: str, org_uid: str) -> Thread:
        # Imports local to avoid cycles, mirroring api/v1/tickets.py.
        from domains.agent_overlays.services.composition import compose_playbook_intent
        from domains.investigations.schemas import InvestigationEffort, RunTrigger
        from domains.investigations.services.lifecycle import LifecycleError, trigger_run
        from domains.run_policies.services.effort import ensure_policy_for_effort
        from domains.tickets.services.ticket_service import TicketService

        ticket = await TicketService().get_node(ticket_uid)
        existing = await self.list(subject_ticket_uid=ticket_uid)
        if has_active_thread(existing):
            raise HTTPException(status_code=409, detail="ticket already has an active thread")

        thread = Thread(
            uid=uuid4().hex,
            repository_uid=ticket.repository_uid,
            subject_ticket_uid=ticket_uid,
            created_by=actor_uid,
        )
        await thread.save()

        composed = await compose_playbook_intent(
            repository_uid=ticket.repository_uid,
            playbook="refine",
            stage="refine",
            repo_guidance="",
            custom_intent=build_thread_session_intent(ticket, thread.uid),
            org_uid=org_uid,
        )
        policy = await ensure_policy_for_effort(InvestigationEffort.NORMAL)
        try:
            run = await trigger_run(
                repository_uid=ticket.repository_uid,
                intent=composed.text,
                playbook="refine",
                title=f"Thread: {(ticket.title or 'ticket')[:80]}",
                target={"thread_uid": thread.uid, "ticket_uid": ticket_uid},
                linked_ticket_uid=ticket_uid,
                run_policy_uid=policy.uid,
                trigger=RunTrigger.MANUAL,
                triggered_by=actor_uid,
            )
        except LifecycleError as exc:
            await thread.delete()  # dispatch never started — no orphan threads
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await self.attach_run(thread, run.uid)
        await write_audit(
            kind="thread.created",
            subject_uid=thread.uid,
            subject_type="Thread",
            actor_uid=actor_uid,
            payload={"ticket_uid": ticket_uid, "run_uid": run.uid},
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
        from domains.delivery.services.implement_run_service import trigger_implement_run
        from domains.investigations.services.run_events import read_events
        from domains.tickets.services.ticket_service import TicketService

        t = await self.get_node(uid)
        if t.phase != "refining":
            raise HTTPException(status_code=409, detail=f"thread is {t.phase}, not refining")
        ticket = await TicketService().get_node(t.subject_ticket_uid)

        events: list[dict] = []
        if t.active_run_uid:
            try:
                events = read_events(t.active_run_uid)
            except Exception:  # noqa: BLE001 — carry-over is best-effort
                logger.warning(
                    f"thread {uid}: could not read session events for carry-over",
                    extra={"tag": "threads"},
                )
        addendum = compose_addendum_for_thread(t.plan_state, t.plan_text or "", events)

        # Group flow: a parent ticket's thread implements the whole batch in
        # one branch/PR — inline the subtickets into the intent.
        from domains.threads.services.intents import build_group_addendum
        from domains.tickets.models import Ticket

        children = list(await Ticket.nodes.filter(parent_ticket_uid=ticket.uid))
        addendum += build_group_addendum(children)

        # trigger_implement_run raises HTTPException(409) itself when Gate 1
        # hasn't passed or a PR already exists — let those propagate untouched.
        run = await trigger_implement_run(
            ticket, triggered_by=actor_uid, intent_addendum=addendum
        )
        await self.attach_run(t, run.uid)
        await self.transition(uid, "implementing", actor_uid=actor_uid)
        return run
