"""Run routes — the ONE execution surface (PLATFORM_V3_DESIGN.md §9).

A Run is a conversation with an agent in a workspace. This router covers:
- creation of one-off chat/ask runs (domain triggers own review/fix/
  implement/verify — they carry the guards),
- listing / detail / structured transcript,
- live streaming: the WS tails the run's event stream (all turns, whichever
  process executes them) and carries follow-up turns; REST is the fallback,
- interrupt / end / workspace recreation.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from api.dependencies import get_current_user, require_role
from domains.runs.models import Run
from domains.runs.schemas import (
    ClientWsMessage,
    CreateRunRequest,
    Playbook,
    RunDTO,
    RunMessageResult,
    RunStatus,
    SendRunMessageRequest,
)
from domains.runs.services.active_runs import active_runs_for
from domains.runs.services.lifecycle import LifecycleError, trigger_run
from domains.runs.services.run_changes import read_changes
from domains.runs.services.run_events import (
    append_event,
    read_events,
    read_events_from,
    run_events_channel,
)
from domains.runs.services.run_reconciliation import reconcile_stale_runs
from domains.runs.services.turn_service import TurnService, run_to_dto
from domains.runs.services.workspace import (
    WorkspaceError,
    recreate_workspace,
)
from domains.repositories.services.repository_service import RepositoryService
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.users.schemas import UserDTO
from logging_config import logger
from redis_config import get_redis_url

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

# Statuses after which a run's transcript can no longer grow without a
# follow-up message.
_DONE_STATUSES = {"awaiting_input", "ended", "failed", "cancelled", "limit_exceeded"}


@router.post("", response_model=RunDTO, operation_id="opensweep_run_create")
async def create_run(req: CreateRunRequest, user: UserDTO = Depends(require_role("maintainer"))):
    """Create a one-off run.

    - playbook=ask: dispatches through the executor adapter (findings machinery).
    - playbook=chat: a conversation-only run — a discovery workspace is
      cloned in the background; the first message (req.prompt, optional) runs
      as turn one (V3 §8).
    """
    if req.surface not in {"runs", "chat"}:
        raise HTTPException(status_code=422, detail=f"unknown surface {req.surface!r}")
    if req.surface == "chat" and req.playbook != Playbook.CHAT:
        raise HTTPException(
            status_code=422, detail="surface=chat requires playbook=chat"
        )
    # A chat started from a subject page may omit the repository — the
    # subject's repository is the natural workspace.
    if not req.repository_uid and req.surface == "chat":
        req.repository_uid = await _repo_from_context(req.context) or ""
    if not req.repository_uid:
        raise HTTPException(status_code=422, detail="repository_uid is required")
    # Tenancy: the target repository must be the caller's — 404 otherwise
    # (both playbook paths; chat re-checks inside _create_chat_run).
    await require_repo_in_org(req.repository_uid, user.org_uid)
    if req.playbook == Playbook.ASK:
        # Org-agent-overlays composition: the user's prompt (custom_intent)
        # or the platform ask instructions, with the org overlay applied.
        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=req.repository_uid,
            agent_key="ask",
            stage="ask",
            repo_guidance="",
            custom_intent=(req.prompt or "").strip() or None,
            org_uid=user.org_uid,
        )
        try:
            run = await trigger_run(
                repository_uid=req.repository_uid,
                intent=composed.text,
                playbook="ask",
                title=req.title or "",
                target=dict(req.target or {}),
                linked_pr_uid=req.linked_pr_uid,
                linked_ticket_uid=req.linked_ticket_uid,
                linked_finding_uid=req.linked_finding_uid,
                executor=req.executor,
                triggered_by=user.uid,
            )
        except LifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return run_to_dto(run)
    if req.playbook != Playbook.CHAT:
        raise HTTPException(
            status_code=422,
            detail=f"POST /runs creates chat or ask runs; {req.playbook.value} runs have their own trigger endpoint",
        )
    return run_to_dto(await _create_chat_run(req, actor_uid=user.uid, org_uid=user.org_uid))


async def _repo_from_context(context: dict[str, str] | None) -> str | None:
    """The repository of a chat's context subject — None when unresolvable."""
    subject_type = (context or {}).get("subject_type", "").strip()
    subject_uid = (context or {}).get("subject_uid", "").strip()
    if not subject_type or not subject_uid:
        return None
    try:
        from domains.comments.schemas import CommentSubjectType
        from domains.comments.subjects import subject_repository_uid

        return await subject_repository_uid(CommentSubjectType(subject_type), subject_uid)
    except ValueError:
        return None


async def _create_chat_run(req: CreateRunRequest, *, actor_uid: str, org_uid: str) -> Run:
    from domains.runs.services import workspace as workspace_service
    from domains.llm_providers.services.llm_provider_service import select_provider
    from domains.execution.services.sandbox_service import SandboxService
    from domains.runs.services.lifecycle import _executor_for_provider
    from domains.repositories.services.repository_service import repository_to_dto

    repository = await RepositoryService().get_repository(req.repository_uid, org_uid)
    provider = await select_provider(org_uid=org_uid)
    if provider is None:
        raise HTTPException(
            status_code=409,
            detail="No LLM provider configured for your organization — add one in Settings → LLM Providers and mark it active.",
        )
    try:
        executor = req.executor or _executor_for_provider(provider)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # A PR discussion works on the PR's head branch (with the base fetched so
    # `git diff base...head` resolves) — a default-branch clone can't see the
    # diff being discussed. Recorded in target so workspace recreation keeps it.
    source_branch: str | None = None
    extra_refs: list[str] = []
    target = dict(req.target or {})
    if req.linked_pr_uid:
        from domains.delivery.models import PullRequest

        pr = await PullRequest.nodes.get_or_none(uid=req.linked_pr_uid)
        if pr is not None:
            source_branch = (pr.head_ref or "").strip() or None
            base_ref = (pr.base_ref or "").strip()
            if base_ref:
                extra_refs = [base_ref]
            if source_branch:
                target.setdefault("head_ref", source_branch)
            if base_ref:
                target.setdefault("base_ref", base_ref)

    # OpenSweep chat-bubble sessions record what the user was viewing; the
    # snapshot lands in the first turn's preamble below.
    context = {k: v for k, v in (req.context or {}).items() if v}
    if req.surface == "chat" and context:
        target.setdefault("subject_type", context.get("subject_type", ""))
        target.setdefault("subject_uid", context.get("subject_uid", ""))

    # Agent provenance (chat runs bypass trigger_run).
    from domains.agents.services.agent_service import active_agent_provenance

    agent_uid, agent_rev = await active_agent_provenance(org_uid, "chat")

    now = datetime.now(UTC)
    run = Run(
        uid=uuid4().hex,
        repository_uid=repository.uid,
        playbook=Playbook.CHAT.value,
        title=req.title or f"Chat on {repository.slug}",
        executor=executor.value,
        execution_mode="analyze_only",
        provider_uid=(provider.uid or "").strip(),
        agent_uid=agent_uid,
        agent_rev=agent_rev,
        status=RunStatus.QUEUED.value,
        linked_pr_uid=req.linked_pr_uid or "",
        linked_ticket_uid=req.linked_ticket_uid or "",
        linked_finding_uid=req.linked_finding_uid or "",
        target=target,
        surface=req.surface,
        triggered_by=actor_uid,
        started_at=now,
        last_activity_at=now,
        usage={
            "provider_uid": (provider.uid or "").strip(),
            "provider_kind": (provider.kind or "").strip(),
        },
    )
    await run.save()

    first_prompt = (req.prompt or "").strip()
    if first_prompt and req.surface == "chat":
        from domains.runs.services.chat_context import build_chat_preamble

        preamble = await build_chat_preamble(context, org_uid=org_uid)
        first_prompt = f"{preamble}\n\n## The maintainer says\n{first_prompt}"

    async def _prepare() -> None:
        try:
            sandbox = await SandboxService().create_for_discovery(
                repository=repository_to_dto_safe(repository),
                agent_run_uid=run.uid,
                source_branch=source_branch,
                extra_refs=extra_refs or None,
            )
        except Exception as exc:  # noqa: BLE001
            fresh = await Run.nodes.get_or_none(uid=run.uid)
            if fresh is None:
                return
            fresh.status = RunStatus.FAILED.value
            fresh.error = f"workspace prep failed: {exc}"[:500]
            fresh.updated_at = datetime.now(UTC)
            await fresh.save()
            append_event(run.uid, "error", detail=fresh.error)
            return
        fresh = await Run.nodes.get_or_none(uid=run.uid)
        if fresh is None:
            return
        fresh.sandbox_uid = sandbox.uid
        fresh.workspace_spec = workspace_service.build_workspace_spec(
            sandbox, base_branch=extra_refs[0] if extra_refs else ""
        )
        fresh.status = RunStatus.AWAITING_INPUT.value
        fresh.updated_at = datetime.now(UTC)
        await fresh.save()
        append_event(run.uid, "system", kind="sandbox", text="workspace ready")
        if first_prompt:
            try:
                async for _ in TurnService().run_turn(run.uid, first_prompt):
                    pass
            except HTTPException as exc:
                logger.warning(f"chat run {run.uid} first turn failed: {exc.detail}")

    def repository_to_dto_safe(repo_dto):
        # RepositoryService.get_repository already returns a DTO.
        return repo_dto

    task = asyncio.create_task(_prepare())

    def _log_failure(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat run prep crashed for {run.uid}: {exc}")

    task.add_done_callback(_log_failure)
    return run


@router.get("", response_model=list[RunDTO], operation_id="opensweep_list_runs")
async def list_runs(
    repository_uid: str | None = Query(None),
    executor: str | None = Query(None),
    status: str | None = Query(None),
    playbook: str | None = Query(None),
    linked_pr_uid: str | None = Query(None),
    linked_ticket_uid: str | None = Query(None),
    linked_finding_uid: str | None = Query(None),
    surface: str | None = Query(None),
    limit: int = Query(100, le=500),
    user: UserDTO = Depends(get_current_user),
):
    """List runs. Only surface="runs" is returned by default — @opensweep comment
    replies (surface=comment) live in their thread and chat-bubble sessions
    (surface=chat) in the widget. ?surface= widens that: "chat" returns the
    caller's own chat sessions; "comment"/"all" are platform-admin only."""
    if surface is not None and surface not in {"runs", "comment", "chat", "all"}:
        raise HTTPException(status_code=422, detail=f"unknown surface {surface!r}")
    if surface in {"comment", "all"} and not user.is_platform_admin:
        raise HTTPException(
            status_code=403, detail="agent activity surfaces are platform-admin only"
        )
    if repository_uid:
        await require_repo_in_org(repository_uid, user.org_uid)
    allowed = await org_repo_uids(user.org_uid)
    await reconcile_stale_runs()
    nodes = await Run.nodes.all()
    out: list[RunDTO] = []
    for r in nodes:
        r_surface = r.surface or "runs"
        if surface is None:
            if r_surface != "runs":
                continue
        elif surface == "chat":
            # Chat sessions are personal — "chat" ALWAYS means the caller's
            # own (the widget's history). Admin oversight goes through "all".
            if r_surface != "chat" or (r.triggered_by or "") != user.uid:
                continue
        elif surface != "all" and r_surface != surface:
            continue
        if r.repository_uid not in allowed:
            continue
        if repository_uid and r.repository_uid != repository_uid:
            continue
        if executor and r.executor != executor:
            continue
        if status and r.status != status:
            continue
        if playbook and (r.playbook or "") != playbook:
            continue
        if linked_pr_uid and (r.linked_pr_uid or "") != linked_pr_uid:
            continue
        if linked_ticket_uid and (r.linked_ticket_uid or "") != linked_ticket_uid:
            continue
        if linked_finding_uid and (r.linked_finding_uid or "") != linked_finding_uid:
            continue
        out.append(run_to_dto(r))
    out.sort(
        key=lambda x: x.last_activity_at
        or x.started_at
        or x.created_at
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return out[:limit]


class ActiveRunDTO(BaseModel):
    """Minimal linkage record for an in-flight run — enough for the UI to
    show "already running" and deep-link the run."""

    run_uid: str
    scheduled_agent_uid: str = ""
    title: str = ""
    playbook: str = ""
    status: str
    started_at: datetime | None = None
    repository_uid: str = ""


@router.get(
    "/active",
    response_model=list[ActiveRunDTO],
    operation_id="opensweep_list_active_runs",
)
async def list_active_runs(
    repository_uid: str | None = Query(None),
    pull_request_uid: str | None = Query(None),
    ticket_uid: str | None = Query(None),
    finding_uid: str | None = Query(None),
    playbook: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    """Runs currently queued/running/paused_quota so dispatch surfaces can
    point at what is in flight."""
    if repository_uid:
        await require_repo_in_org(repository_uid, user.org_uid)
    allowed = await org_repo_uids(user.org_uid)
    await reconcile_stale_runs()
    runs = await active_runs_for(
        repository_uid=repository_uid,
        pull_request_uid=pull_request_uid,
        ticket_uid=ticket_uid,
        finding_uid=finding_uid,
        playbooks=[playbook] if playbook else None,
    )
    out = [
        ActiveRunDTO(
            run_uid=r.uid,
            scheduled_agent_uid=r.scheduled_agent_uid or "",
            title=r.title or "",
            playbook=r.playbook or "",
            status=r.status or "",
            started_at=r.started_at,
            repository_uid=r.repository_uid or "",
        )
        for r in runs
        # Hidden surfaces (comment replies, chat sessions) never toast or
        # count as "already running" — their own UIs track them live.
        if r.repository_uid in allowed and (r.surface or "runs") == "runs"
    ]
    out.sort(
        key=lambda x: x.started_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return out


class RunTranscriptDTO(BaseModel):
    """Structured transcript events (PLATFORM_V3_DESIGN.md §4)."""

    events: list[dict] = []
    last_seq: int = 0
    done: bool = False


@router.get(
    "/{uid}/transcript",
    response_model=RunTranscriptDTO,
    operation_id="opensweep_run_transcript",
)
async def get_transcript(
    uid: str,
    after_seq: int = Query(0, ge=0),
    user: UserDTO = Depends(get_current_user),
):
    """Incremental read of the run's structured transcript. Poll with the
    returned last_seq; `done` flips while no turn is in flight (a follow-up
    message starts a new turn and the transcript grows again)."""
    r = await Run.nodes.get_or_none(uid=uid)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Run {uid} not found")
    await require_repo_in_org(r.repository_uid, user.org_uid)
    events = read_events(uid, after_seq)
    last_seq = int(events[-1]["seq"]) if events else after_seq
    return RunTranscriptDTO(
        events=events,
        last_seq=last_seq,
        done=(r.status or "") in _DONE_STATUSES,
    )


class RunChangesDTO(BaseModel):
    """File tree + per-file unified diffs of what a run changed (Files tab)."""

    source: str = "none"
    base: str = ""
    captured_at: str | None = None
    files: list[dict] = []
    tree: list[str] = []


@router.get(
    "/{uid}/changes",
    response_model=RunChangesDTO,
    operation_id="opensweep_run_changes",
)
async def get_run_changes(uid: str, user: UserDTO = Depends(get_current_user)):
    """File tree + diffs of what this run changed in its workspace — live
    from the sandbox git state while it exists, from the last snapshot after
    teardown."""
    r = await Run.nodes.get_or_none(uid=uid)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Run {uid} not found")
    await require_repo_in_org(r.repository_uid, user.org_uid)
    return RunChangesDTO(**await read_changes(r))


@router.get("/{uid}", response_model=RunDTO, operation_id="opensweep_get_run")
async def get_run(uid: str, user: UserDTO = Depends(get_current_user)):
    await reconcile_stale_runs()
    r = await Run.nodes.get_or_none(uid=uid)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Run {uid} not found")
    await require_repo_in_org(r.repository_uid, user.org_uid)
    return run_to_dto(r)


@router.post(
    "/{uid}/messages",
    response_model=RunMessageResult,
    operation_id="opensweep_run_send",
)
async def send_message(
    uid: str, req: SendRunMessageRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    """REST fallback for one follow-up turn: runs the shared turn-runner to
    completion and returns the final assistant message (the WS streams the
    same events). Accepted from awaiting_input AND ended/failed/cancelled/
    limit_exceeded — replying to a failed run is the recovery loop (V3 §2)."""
    service = TurnService()
    run = await service.get_run(uid)
    await require_repo_in_org(run.repository_uid, user.org_uid)
    content = ""
    final_status = RunStatus.AWAITING_INPUT.value
    error_detail = ""
    interrupted = False
    async for event in service.run_turn(uid, req.text):
        if event["type"] == "message_complete":
            content = event.get("content") or ""
            interrupted = bool(event.get("interrupted"))
        elif event["type"] == "status":
            final_status = event.get("status") or final_status
        elif event["type"] == "error":
            error_detail = event.get("detail") or ""
    if error_detail and not content:
        raise HTTPException(status_code=502, detail=error_detail)
    return RunMessageResult(
        content=content,
        status=RunStatus(final_status),
        interrupted=interrupted,
        error=error_detail,
    )


@router.post("/{uid}/interrupt", response_model=RunDTO, operation_id="opensweep_run_interrupt")
async def interrupt_run(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Kill the in-flight turn (SIGTERM → SIGKILL after 5s). The run survives
    and accepts the next message. 409 when nothing is running."""
    service = TurnService()
    existing = await service.get_run(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    run = await service.interrupt(uid, actor_uid=user.uid)
    return run_to_dto(run)


@router.post("/{uid}/cancel", response_model=RunDTO, operation_id="opensweep_run_cancel")
async def cancel_run(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Cancel an active (queued/running/paused_quota) run: set the terminal
    `cancelled` status, kill any in-flight turn, and fire the completion hook
    so linked entities never wedge. 409 when the run isn't active."""
    service = TurnService()
    existing = await service.get_run(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    run = await service.cancel_run(uid, actor_uid=user.uid)
    return run_to_dto(run)


@router.post("/{uid}/end", response_model=RunDTO, operation_id="opensweep_run_end")
async def end_run(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """End the run: kill any in-flight turn, destroy the workspace now. The
    transcript is retained; a follow-up message reopens the run."""
    service = TurnService()
    existing = await service.get_run(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    run = await service.end_run(uid, actor_uid=user.uid)
    return run_to_dto(run)


@router.post(
    "/{uid}/workspace/recreate",
    response_model=RunDTO,
    operation_id="opensweep_run_recreate_workspace",
)
async def recreate_run_workspace(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Rebuild the run's workspace from its recorded workspace_spec (V3 §7).
    Follow-up messages do this implicitly; this endpoint is the explicit
    variant."""
    run = await TurnService().get_run(uid)
    await require_repo_in_org(run.repository_uid, user.org_uid)
    try:
        await recreate_workspace(run)
    except WorkspaceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return run_to_dto(await TurnService().get_run(uid))


async def _tail_run_events(
    websocket: WebSocket, uid: str, *, after_seq: int, last_status: str
) -> None:
    """Push transcript events, token deltas and status changes to one
    connected watcher.

    The events FILE is the source of truth for transcript events: this reads
    it incrementally by byte offset, so it streams every event regardless of
    which process (backend or Celery worker) is executing the run. The Redis
    channel carries two payload kinds: appended events (with seq) act as
    doorbells triggering an in-order file flush, and ephemeral {"type":
    "delta"} payloads (publish_delta — claude partial messages) are forwarded
    directly, never stored. When Redis is down the loop degrades to a 1s file
    tick and deltas are simply absent — the message still lands whole.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(get_redis_url(db=0), decode_responses=True)
    pubsub = r.pubsub()
    subscribed = False
    offset = 0
    idle_ticks = 0

    async def _flush_file() -> bool:
        nonlocal offset
        events, offset = read_events_from(uid, offset, after_seq=after_seq)
        for event in events:
            await websocket.send_json({"type": "event", "event": event})
        return bool(events)

    try:
        while True:
            if not subscribed:
                with contextlib.suppress(Exception):
                    await pubsub.subscribe(run_events_channel(uid))
                    subscribed = True
            active = await _flush_file()  # anything appended while we slept
            # Drain the channel: block up to 1s for the first message, then
            # sweep the backlog non-blocking so a token burst doesn't cost
            # one loop iteration per delta. Redis errors only degrade the
            # transport — websocket sends happen outside this try.
            pending: list = []
            if subscribed:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    while msg is not None:
                        pending.append(msg)
                        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
                except Exception:  # noqa: BLE001 — redis died mid-stream
                    subscribed = False
            else:
                await asyncio.sleep(1.0)
            for msg in pending:
                payload = None
                if isinstance(msg.get("data"), str):
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(msg["data"])
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "delta"
                    and "seq" not in payload
                ):
                    text = payload.get("text")
                    if isinstance(text, str) and text:
                        await websocket.send_json({"type": "delta", "text": text})
                        active = True
                else:
                    # Appended-event doorbell — flush at this position in the
                    # stream so deltas of the next message never overtake the
                    # completed message they follow.
                    active = await _flush_file() or active
            # Status frames ride the same loop: re-check when traffic landed,
            # else every few ticks (covers interrupt/reconciliation, which
            # change status without appending an event).
            idle_ticks = 0 if active else idle_ticks + 1
            if idle_ticks % 3 == 0:
                run = await Run.nodes.get_or_none(uid=uid)
                status = (run.status or "") if run else ""
                if status and status != last_status:
                    last_status = status
                    await websocket.send_json({"type": "status", "status": status})
    finally:
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await r.aclose()


@router.websocket("/{uid}/ws")
async def run_ws(websocket: WebSocket, uid: str, after_seq: int = Query(0, ge=0)):
    """Live run transport (V3 §5): transcript stream + conversation turns.

    On connect the server replays transcript events with seq > after_seq and
    keeps pushing new ones as the run produces them — including turns
    dispatched by other processes (initial dispatch, Celery quota resume).

    Client → server: {"type": "message", "text": "..."} | {"type": "interrupt"}
    Server → client: {"type": "event", "event"} (transcript, carries seq) /
    {"type": "delta", "text"} (ephemeral token stream of the in-flight
    message, whoever started the turn) / {"type": "message_complete",
    "content"} / {"type": "status", "status"} / {"type": "error", "detail"}.

    Auth: the shared-token layer authenticates the handshake and closes 4401
    before this handler runs. Watching is read-only, so viewers may connect;
    message/interrupt mutate and are answered with an error frame below
    maintainer.

    Scope note: access here (like /transcript and /messages) is org-wide by
    design, INCLUDING chat-surface runs — the comment thinking bubble needs
    any org member to tail comment-surface runs, and chat privacy is
    list-level only ("chat" listing returns the caller's own sessions).
    """
    from domains.users.schemas import role_at_least

    user = await get_current_user(websocket)
    service = TurnService()
    await websocket.accept()
    try:
        run = await service.get_run(uid)
        await require_repo_in_org(run.repository_uid, user.org_uid)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "detail": exc.detail})
        await websocket.close(code=4404)
        return
    can_write = role_at_least(user.role, "maintainer")
    tail = asyncio.create_task(
        _tail_run_events(websocket, uid, after_seq=after_seq, last_status=run.status or "")
    )
    # A tailer that dies first (client vanished mid-send) must not leave an
    # unretrieved task exception behind.
    tail.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        await websocket.send_json({"type": "status", "status": run.status})

        while True:
            raw = await websocket.receive_json()
            try:
                msg = ClientWsMessage.model_validate(raw)
            except pydantic.ValidationError as exc:
                await websocket.send_json(
                    {"type": "error", "detail": f"invalid message: {exc.errors()[0].get('msg', exc)}"}
                )
                continue
            if not can_write:
                await websocket.send_json(
                    {"type": "error", "detail": "requires role 'maintainer' or higher"}
                )
                continue
            if msg.type == "interrupt":
                try:
                    await service.interrupt(uid)
                except HTTPException as exc:
                    await websocket.send_json({"type": "error", "detail": exc.detail})
                continue
            turn = service.run_turn(uid, msg.text)
            try:
                # Turn-boundary frames only — transcript events and token
                # deltas reach the client through the tailer.
                async for event in turn:
                    await websocket.send_json(event)
            except HTTPException as exc:  # guard failures (busy/ended) → error event
                await websocket.send_json({"type": "error", "detail": exc.detail})
            finally:
                # Explicit aclose: if the socket died mid-stream, this raises
                # GeneratorExit inside run_turn, which kills the subprocess
                # and keeps the run followable (no GC-timing reliance).
                await turn.aclose()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — never let a WS error kill the app
        logger.warning(f"run ws {uid}: {exc}", extra={"tag": "runs"})
        try:
            await websocket.send_json({"type": "error", "detail": "internal error"})
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        tail.cancel()
