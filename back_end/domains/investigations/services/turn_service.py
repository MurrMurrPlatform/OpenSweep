"""Follow-up turns on runs (PLATFORM_V3_DESIGN.md §6).

One follow-up turn = one CLI subprocess (claude/codex) or one provider
invocation (internal_llm/opencode) in the run's workspace. The runner is an
async generator of turn-boundary events ({"type": "status"|"error"|
"message_complete", …}); the WebSocket forwards them as they arrive and the
REST fallback (`POST /runs/{uid}/messages`) drains them and returns the
final assistant message — both paths share this code. Transcript CONTENT is
not yielded: appended events and published token deltas reach every watcher
through the run WS tailer (runs.py), the sending socket included.

Follow-ups are accepted from awaiting_input AND ended/failed/cancelled/
limit_exceeded (V3 §2) — replying to a failed run is the recovery loop. The
workspace is ensured first (recreated from workspace_spec when destroyed).

Concurrency: ONE in-flight message per run (409 otherwise). Running
subprocess handles live in a module-level dict so interrupt() can kill the
current turn from another request. Single backend process assumption
(accepted limitation carried over from V2 sessions).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import HTTPException

from config import settings
from domains.executors.mcp_bridge import claude_env, codex_mcp_overrides, write_claude_mcp_config
from domains.executors.stream_events import ClaudeStreamTranslator, stream_event_delta
from domains.investigations.models import Run
from domains.investigations.schemas import FOLLOW_UP_STATUSES, RunStatus
from domains.investigations.services import playbooks as playbook_registry
from domains.investigations.services import run_changes
from domains.investigations.services import workspace as workspace_service
from domains.investigations.services.run_context import build_run_context
from domains.investigations.services.run_events import append_event, publish_delta, read_events
from domains.investigations.services.turn_cli import (
    INTERRUPT_GRACE_SECONDS,
    TURN_TIMEOUT_SECONDS,
    build_claude_turn_argv,
    build_codex_prompt,
    build_codex_turn_argv,
    codex_turn_env,
    extract_claude_meta,
    parse_codex_deltas,
)
from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services.credentials import provider_secret
from infrastructure.audit import write_audit
from infrastructure.code_graph import CODE_GRAPH_PROMPT, code_graph_available
from infrastructure.process_tree import kill_tree, process_group_kwargs, terminate_tree
from logging_config import logger

_FOLLOW_UP_STATUS_VALUES = {s.value for s in FOLLOW_UP_STATUSES}

# Executors that run follow-up turns as CLI subprocesses in the workspace.
_SUBPROCESS_EXECUTORS = {"claude_code", "codex"}

# First-message queueing: how long a held message waits for the chat run's
# background workspace clone before giving up.
_PREP_WAIT_TIMEOUT_SECONDS = 600
_PREP_POLL_SECONDS = 1.0


class _Starting:
    """Sentinel registered in _RUNNING between the busy-guard and the actual
    subprocess spawn, so a concurrent send sees the slot as taken (409)."""


_STARTING = _Starting()

# Running turn subprocesses, keyed by run uid. Module-level on purpose:
# interrupt() must reach the process spawned by a different request handler.
_RUNNING: dict[str, asyncio.subprocess.Process | _Starting] = {}
# Runs whose current turn was interrupted (kill in flight).
_INTERRUPTED: set[str] = set()
# Per-run locks serializing the busy-guard check + _RUNNING reservation.
_SEND_LOCKS: dict[str, asyncio.Lock] = {}


async def _kill_turn_process(proc: asyncio.subprocess.Process) -> None:
    """Interrupt semantics shared by interrupt(), WS disconnect, and REST
    cancellation: SIGTERM, SIGKILL after the grace period. Signals reach the
    CLI's whole process group — a CLI-only kill orphans its MCP bridge /
    Bash-tool children, which then pin backend fds forever (EMFILE)."""
    try:
        terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=INTERRUPT_GRACE_SECONDS)
        except TimeoutError:
            kill_tree(proc)
    except ProcessLookupError:
        pass  # already exited


# Statuses a cancel is accepted from: the run is (or will be) doing work.
# queued → the dispatch pipeline's != QUEUED check aborts pre-dispatch;
# running → the late adapter result is discarded by _dispatch_and_finalize's
# status re-check; paused_quota → the resume task only touches paused_quota.
CANCELLABLE_STATUSES = frozenset(
    {RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.PAUSED_QUOTA.value}
)


def ensure_can_cancel(status: str) -> None:
    """Guard for run cancellation — pure so it stays testable without Neo4j."""
    if status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"run is {status}; only queued/running/paused_quota runs can be cancelled",
        )


def ensure_can_send(status: str, in_flight: bool, *, playbook: str = "") -> None:
    """Guard for follow-up messages: pure so it stays testable without Neo4j.

    A queued CHAT run accepts the message — `queued` there means the
    background workspace clone is still running, and run_turn holds the
    message until prep finishes (first-message queueing) instead of bouncing
    the user with a 409.
    """
    if in_flight:
        raise HTTPException(
            status_code=409,
            detail="a message is already in flight for this run — interrupt it or wait",
        )
    if status == RunStatus.QUEUED.value and playbook == "chat":
        return
    if status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        raise HTTPException(
            status_code=409,
            detail="the run is working — wait for the turn to finish or interrupt it",
        )
    if status == RunStatus.PAUSED_QUOTA.value:
        raise HTTPException(
            status_code=409,
            detail="the run is paused on provider quota — it resumes automatically",
        )
    if status not in _FOLLOW_UP_STATUS_VALUES:
        raise HTTPException(status_code=409, detail=f"run is {status}; cannot accept messages")


def transcript_entries(run_uid: str, *, limit: int = 200) -> list[dict]:
    """Conversation entries (role/content) from the run's event stream — the
    codex/internal_llm reseed context and the workspace-recreation fallback."""
    entries: list[dict] = []
    for e in read_events(run_uid, 0, limit=10_000):
        if e.get("type") == "user_message":
            entries.append({"role": "user", "content": e.get("text") or ""})
        elif e.get("type") == "assistant_text":
            if entries and entries[-1]["role"] == "assistant":
                entries[-1]["content"] += e.get("text") or ""
            else:
                entries.append({"role": "assistant", "content": e.get("text") or ""})
    return entries[-limit:]


class TurnService:
    async def get_run(self, uid: str) -> Run:
        run = await Run.nodes.get_or_none(uid=uid)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {uid} not found")
        return run

    # ── Turn runner (shared by WS and REST) ──────────────────────────────────

    async def run_turn(self, uid: str, text: str) -> AsyncGenerator[dict, None]:
        """Run one follow-up message as one turn; yields protocol events."""
        lock = _SEND_LOCKS.setdefault(uid, asyncio.Lock())
        async with lock:
            run = await self.get_run(uid)
            ensure_can_send(run.status or "", uid in _RUNNING, playbook=run.playbook or "")
            _RUNNING[uid] = _STARTING
            _INTERRUPTED.discard(uid)

        # First-message queueing: a chat run stays `queued` while its
        # workspace clones in the background — hold the reserved turn slot
        # and start the turn the moment prep finishes. Concurrent sends see
        # the slot as taken (409) while we wait.
        if (run.status or "") == RunStatus.QUEUED.value:
            run = await self._wait_for_chat_prep(uid)

        turn_no = int(run.turns or 0) + 1
        deltas: list[str] = []
        result_text: str | None = None
        cli_session_id = ""
        stderr_parts: list[str] = []
        error_detail = ""
        argv: list[str] = []

        proc: asyncio.subprocess.Process | None = None
        setup_ok = False
        try:
            provider = (
                await LLMProvider.nodes.get_or_none(uid=run.provider_uid)
                if run.provider_uid
                else None
            )

            # Ensure the workspace BEFORE flipping to running — recreation is
            # a clone and can fail; the run must stay followable.
            cwd: str | None = None
            if (run.executor or "") in _SUBPROCESS_EXECUTORS:
                try:
                    cwd = await workspace_service.ensure_workspace(run)
                except workspace_service.WorkspaceError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                run = await self.get_run(uid)  # recreation may have saved fields
            elif (run.executor or "") not in {"internal_llm"}:
                # opencode runs also work in a workspace when one exists.
                try:
                    cwd = await workspace_service.ensure_workspace(run)
                except workspace_service.WorkspaceError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                run = await self.get_run(uid)

            now = datetime.now(UTC)
            reopened = run.status == RunStatus.ENDED.value
            run.status = RunStatus.RUNNING.value
            run.error = ""
            run.updated_at = now
            run.last_activity_at = now
            await run.save()
            yield {"type": "status", "status": RunStatus.RUNNING.value}
            if reopened:
                append_event(uid, "system", kind="reopened", text="run reopened by a follow-up message", turn=turn_no)
            append_event(uid, "user_message", text=text, turn=turn_no)

            if (run.executor or "") in _SUBPROCESS_EXECUTORS:
                argv, env = await self._build_subprocess_turn(run, provider, text, cwd=cwd)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=cwd,
                        limit=16 * 1024 * 1024,
                        # Group leader, so interrupt/timeout kills reach the
                        # CLI's MCP/Bash children too (see process_tree).
                        **process_group_kwargs(),
                    )
                except FileNotFoundError as exc:
                    error_detail = f"{argv[0]} CLI not found: {exc}"
                except OSError as exc:
                    error_detail = f"failed to spawn {argv[0]}: {exc}"
            setup_ok = True
        except (GeneratorExit, asyncio.CancelledError):
            # Consumer vanished / request cancelled before the spawn.
            await self._finalize_turn(
                uid,
                turn_no=turn_no,
                deltas=deltas,
                result_text=result_text,
                cli_session_id=cli_session_id,
                error_detail="",
                exit_code=None,
            )
            raise
        finally:
            if proc is not None:
                _RUNNING[uid] = proc
            elif not setup_ok or (run.executor or "") in _SUBPROCESS_EXECUTORS:
                # No subprocess was spawned and the turn is over (setup raised
                # — any executor — or a subprocess spawn failed): free the
                # _STARTING reservation, or every follow-up would 409 forever.
                # For internal_llm/opencode with successful setup the
                # reservation stays: it is the busy marker for the provider
                # turn below, popped in that block's own finally.
                if _RUNNING.get(uid) is _STARTING:
                    _RUNNING.pop(uid, None)

        if (run.executor or "") not in _SUBPROCESS_EXECUTORS:
            # internal_llm / opencode: one provider invocation, no PTY stream.
            # cwd is the ensured workspace for opencode, None for internal_llm.
            try:
                content = await self._run_provider_turn(run, provider, text, cwd=cwd)
                deltas.append(content)
            except Exception as exc:  # noqa: BLE001
                error_detail = f"{type(exc).__name__}: {exc}"
            finally:
                _RUNNING.pop(uid, None)
        elif proc is not None:
            translator = ClaudeStreamTranslator()
            is_claude = (run.executor or "") == "claude_code"

            async def _drain_stderr() -> None:
                assert proc is not None and proc.stderr is not None
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        return
                    stderr_parts.append(line.decode("utf-8", errors="replace"))

            stderr_task = asyncio.create_task(_drain_stderr())
            try:
                async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                    assert proc.stdout is not None
                    while True:
                        raw = await proc.stdout.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", errors="replace")
                        # Transcript content is NOT yielded here: appended
                        # events and published deltas reach every watcher —
                        # including the socket that sent this message —
                        # through the run WS tailer, so yielding them too
                        # would double-render.
                        if is_claude:
                            partial = stream_event_delta(line)
                            if partial is not None:
                                # Ephemeral token delta — fan out, never store.
                                if partial:
                                    publish_delta(uid, partial, turn=turn_no)
                                continue
                            meta = extract_claude_meta(line)
                            if meta.session_id:
                                cli_session_id = meta.session_id
                            if meta.is_result and meta.result_text is not None:
                                result_text = meta.result_text
                            for event in translator.translate(line):
                                etype = event.pop("type")
                                append_event(uid, etype, turn=turn_no, **event)
                                if etype == "assistant_text":
                                    deltas.append(event.get("text") or "")
                        else:
                            for delta in parse_codex_deltas(line):
                                append_event(uid, "assistant_text", text=delta, turn=turn_no)
                                deltas.append(delta)
                    await proc.wait()
            except TimeoutError:
                error_detail = f"turn timed out after {TURN_TIMEOUT_SECONDS}s"
                _INTERRUPTED.add(uid)
                kill_tree(proc)
                await proc.wait()
            except (GeneratorExit, asyncio.CancelledError):
                # Consumer vanished mid-turn — treat as interrupt: kill the
                # turn, keep the run followable. No yields past this point.
                _INTERRUPTED.add(uid)
                await _kill_turn_process(proc)
                _RUNNING.pop(uid, None)
                stderr_task.cancel()
                await self._finalize_turn(
                    uid,
                    turn_no=turn_no,
                    deltas=deltas,
                    result_text=result_text,
                    cli_session_id=cli_session_id,
                    error_detail="",
                    exit_code=proc.returncode,
                )
                raise
            finally:
                _RUNNING.pop(uid, None)
                stderr_task.cancel()

        exit_code = proc.returncode if proc is not None else None
        interrupted, final_status, content = await self._finalize_turn(
            uid,
            turn_no=turn_no,
            deltas=deltas,
            result_text=result_text,
            cli_session_id=cli_session_id,
            error_detail=error_detail,
            exit_code=exit_code,
        )

        if not error_detail and not interrupted and exit_code not in (0, None):
            stderr_tail = "".join(stderr_parts)[-2000:]
            program = argv[0] if argv else (run.executor or "agent")
            error_detail = f"{program} exited {exit_code}" + (f": {stderr_tail}" if stderr_tail else "")
        if error_detail:
            yield {"type": "error", "detail": error_detail}
        yield {"type": "message_complete", "content": content, "interrupted": interrupted}
        yield {"type": "status", "status": final_status}

    async def _finalize_turn(
        self,
        uid: str,
        *,
        turn_no: int,
        deltas: list[str],
        result_text: str | None,
        cli_session_id: str,
        error_detail: str,
        exit_code: int | None,
    ) -> tuple[bool, str, str]:
        """Persist the turn outcome; returns (interrupted, final_status, content).

        Turn failures are recoverable: the run goes back to awaiting_input
        with the error recorded so the user can reply — that IS the recovery
        loop. Interrupted turns keep their partial text.
        """
        interrupted = uid in _INTERRUPTED
        _INTERRUPTED.discard(uid)

        run = await Run.nodes.get_or_none(uid=uid)
        content = result_text if result_text else "".join(deltas)
        if run is None:  # deleted mid-turn
            return interrupted, RunStatus.ENDED.value, content

        if cli_session_id:
            run.cli_session_id = cli_session_id

        if run.status in {RunStatus.ENDED.value, RunStatus.CANCELLED.value}:
            # ended/cancelled mid-turn are deliberate terminal decisions — the
            # finalize must not resurrect the run to awaiting_input.
            final_status = run.status
        else:
            final_status = RunStatus.AWAITING_INPUT.value
        run.status = final_status
        run.error = (error_detail or "")[:500]
        if not error_detail and exit_code not in (0, None) and not interrupted:
            run.error = f"last turn exited {exit_code}"
        now = datetime.now(UTC)
        run.turns = turn_no
        run.updated_at = now
        run.last_activity_at = now
        run.completed_at = now
        await run.save()
        if interrupted:
            append_event(uid, "system", kind="interrupt", text="turn interrupted", turn=turn_no)
        append_event(
            uid,
            "turn_end",
            turn=turn_no,
            status="interrupted" if interrupted else ("error" if run.error else "success"),
            usage={"exit_code": exit_code} if exit_code is not None else {},
        )
        await workspace_service.touch_workspace(run)
        await run_changes.snapshot_changes(run)
        # Per-turn completion hooks (V3 §3): a follow-up fix turn that
        # produced new commits is validated and pushed like the first one.
        if not interrupted:
            await playbook_registry.on_turn_complete(run)
        return interrupted, final_status, content

    # ── Interrupt / end ──────────────────────────────────────────────────────

    async def interrupt(self, uid: str, *, actor_uid: str | None = None) -> Run:
        """Kill the current turn (SIGTERM, SIGKILL after grace). The run
        survives: interrupted runs accept the next message."""
        proc = _RUNNING.get(uid)
        if proc is None:
            raise HTTPException(status_code=409, detail="no message is in flight for this run")
        if isinstance(proc, _Starting):
            raise HTTPException(
                status_code=409, detail="the turn is still starting — retry the interrupt"
            )
        _INTERRUPTED.add(uid)
        await _kill_turn_process(proc)
        run = await self.get_run(uid)
        await write_audit(
            kind="run.interrupted", subject_uid=uid, subject_type="Run", actor_uid=actor_uid
        )
        return run

    async def end_run(self, uid: str, *, actor_uid: str | None = None) -> Run:
        """End the run: kill any in-flight turn, destroy the workspace now.
        The transcript is retained; a follow-up message reopens the run."""
        run = await self.get_run(uid)
        if run.status == RunStatus.ENDED.value:
            return run
        proc = _RUNNING.get(uid)
        if proc is not None:
            _INTERRUPTED.add(uid)
            if not isinstance(proc, _Starting):
                kill_tree(proc)
        # Snapshot the diff BEFORE the workspace is torn down so the Files
        # tab survives the teardown (snapshot_changes swallows all errors).
        await run_changes.snapshot_changes(run)
        if run.sandbox_uid:
            try:
                from domains.execution.services.sandbox_service import SandboxService

                await SandboxService().destroy(run.sandbox_uid, actor_uid=actor_uid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"run {uid}: workspace destroy failed: {exc}", extra={"tag": "runs"})
            run.sandbox_uid = ""
        now = datetime.now(UTC)
        run.status = RunStatus.ENDED.value
        run.ended_at = now
        run.updated_at = now
        await run.save()
        append_event(uid, "system", kind="run_status", text="run ended")
        await write_audit(kind="run.ended", subject_uid=uid, subject_type="Run", actor_uid=actor_uid)
        return run

    async def cancel_run(self, uid: str, *, actor_uid: str | None = None) -> Run:
        """Cancel an active (queued/running/paused_quota) run.

        Kills any tracked in-flight turn subprocess; a first-turn subprocess
        owned by an executor adapter isn't tracked here — its late result is
        discarded by _dispatch_and_finalize's status re-check. The playbook
        completion hook fires so linked entities (e.g. a verify verdict) never
        wedge on the cancelled run.
        """
        run = await self.get_run(uid)
        ensure_can_cancel(run.status or "")
        # Persist the cancel BEFORE killing the turn: the dying turn's
        # finalize re-reads the run and preserves cancelled (never resurrects
        # it to awaiting_input).
        now = datetime.now(UTC)
        run.status = RunStatus.CANCELLED.value
        run.completed_at = now
        if run.started_at and not run.duration_ms:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        run.updated_at = now
        run.last_activity_at = now
        await run.save()
        proc = _RUNNING.get(uid)
        if proc is not None:
            _INTERRUPTED.add(uid)
            if not isinstance(proc, _Starting):
                await _kill_turn_process(proc)
        append_event(uid, "system", kind="run_status", text="run cancelled")
        await write_audit(
            kind="run.cancelled", subject_uid=uid, subject_type="Run", actor_uid=actor_uid
        )
        await playbook_registry.on_turn_complete(run)
        return run

    # ── Internals ────────────────────────────────────────────────────────────

    async def _wait_for_chat_prep(self, uid: str) -> Run:
        """Hold the reserved turn slot while the chat run's background
        workspace prep finishes; returns the fresh run. Frees the slot and
        raises when prep fails, times out, or the caller vanishes — the run
        stays `queued`/`failed` with nothing to finalize."""
        deadline = asyncio.get_event_loop().time() + _PREP_WAIT_TIMEOUT_SECONDS
        try:
            while True:
                run = await self.get_run(uid)
                status = run.status or ""
                if status == RunStatus.FAILED.value:
                    raise HTTPException(
                        status_code=502,
                        detail=run.error or "workspace prep failed — recreate the workspace",
                    )
                if status != RunStatus.QUEUED.value:
                    return run
                if asyncio.get_event_loop().time() > deadline:
                    raise HTTPException(
                        status_code=504,
                        detail="the workspace is still being prepared — retry the message",
                    )
                await asyncio.sleep(_PREP_POLL_SECONDS)
        except BaseException:
            _RUNNING.pop(uid, None)
            raise

    async def _build_subprocess_turn(
        self, run: Run, provider, text: str, *, cwd: str | None = None
    ) -> tuple[list[str], dict[str, str]]:
        model = (getattr(provider, "model", "") or "") if provider is not None else ""
        # The briefing must match the tools: both CLI paths below expose the
        # code-graph server exactly when the workspace + binary exist.
        has_code_graph = code_graph_available(cwd or "")
        context = await self._system_prompt(run, code_graph=has_code_graph)
        if (run.executor or "") == "codex":
            entries = transcript_entries(run.uid)
            prompt = build_codex_prompt(text, entries, system_prompt=context)
            argv = build_codex_turn_argv(
                prompt=prompt,
                model=model,
                config_overrides=codex_mcp_overrides(run_uid=run.uid, workspace_path=cwd or ""),
            )
            env = codex_turn_env(provider, run_uid=run.uid)
            return argv, env

        # claude_code (default). workspace_path keeps the code-graph MCP
        # server in the per-run mcp.json on EVERY turn — the config file is
        # shared across turns, so omitting it here would strip the server
        # the first turn exposed.
        scratch_root = getattr(settings, "OPENSWEEP_SANDBOX_HOST_MOUNT", "/tmp") or "/tmp"
        mcp_config_path = write_claude_mcp_config(
            run_uid=run.uid, scratch_root=scratch_root, workspace_path=cwd or ""
        )
        if not mcp_config_path:
            logger.warning(
                f"run {run.uid}: MCP bridge config could not be written",
                extra={"tag": "runs"},
            )
        argv = build_claude_turn_argv(
            text=text,
            cli_session_id=run.cli_session_id or "",
            mcp_config_path=mcp_config_path,
            model=model,
            system_prompt=context,
        )
        oauth_token = provider_secret(provider) if provider is not None else ""
        env = claude_env(run_uid=run.uid, oauth_token=oauth_token)
        return argv, env

    async def _system_prompt(self, run: Run, *, code_graph: bool = False) -> str:
        """Per-turn system prompt: the linked-entity briefing, plus the write
        rules when the run works in a write workspace, plus the code-graph
        briefing when the caller actually exposes the server (never tell the
        agent about tools it doesn't have). Persisted into
        usage["rendered_system_prompt"] so the run detail page shows exactly
        what the agent was told."""
        context = await build_run_context(run)
        if (run.execution_mode or "") == "implement":
            context += (
                "\n\nThis run works in a WRITE workspace: you may edit, test, and COMMIT "
                "inside the working copy. NEVER push, never switch branches, never rewrite "
                "history — the platform validates your commits and pushes after each turn."
            )
        if code_graph:
            context += "\n\n" + CODE_GRAPH_PROMPT
        usage = dict(run.usage or {})
        if usage.get("rendered_system_prompt") != context:
            usage["rendered_system_prompt"] = context
            run.usage = usage
            try:
                await run.save()
            except Exception as exc:  # noqa: BLE001 — display-only, never turn-fatal
                logger.warning(
                    f"run {run.uid}: persisting rendered system prompt failed: {exc}",
                    extra={"tag": "runs"},
                )
        return context

    async def _run_provider_turn(self, run: Run, provider, text: str, *, cwd: str | None) -> str:
        """internal_llm / opencode follow-up: one provider invocation seeded
        with the transcript tail (no CLI resume). Deltas land in the
        transcript via events; the reply returns whole."""
        from domains.llm_providers.services.llm_executor import invoke as invoke_provider

        if provider is None:
            raise HTTPException(
                status_code=409,
                detail="the run's provider is gone — activate a provider and retry",
            )
        # opencode's generated config registers the code-graph server when the
        # invocation has a workspace (llm_executor._prepare_opencode_config);
        # internal_llm runs with cwd=None and no tools, so the gate stays off.
        has_code_graph = (
            (getattr(provider, "kind", "") or "").strip() == "opencode"
            and code_graph_available(cwd or "")
        )
        context = await self._system_prompt(run, code_graph=has_code_graph)
        prompt = build_codex_prompt(text, transcript_entries(run.uid), system_prompt="")
        streamed_len = {"stdout": 0}

        async def _on_chunk(stream: str, chunk_text: str) -> None:
            if stream == "stdout":
                delta = chunk_text[streamed_len["stdout"]:]
                if delta:
                    streamed_len["stdout"] = len(chunk_text)
                    append_event(run.uid, "assistant_text", text=delta, turn=int(run.turns or 0) + 1)

        inv = await invoke_provider(
            provider,
            system_prompt=context,
            instruction=prompt,
            timeout_seconds=await _provider_turn_timeout_seconds(run, provider),
            working_dir=cwd,
            on_chunk=_on_chunk,
            run_uid=run.uid,
        )
        if inv.error:
            raise HTTPException(status_code=502, detail=inv.error)
        return inv.raw_output or ""


async def _provider_turn_timeout_seconds(run: Run, provider) -> int | None:
    """Wall ceiling for a follow-up provider turn — the same ladder the
    dispatch adapters apply: per-stage workflow override → local-kind skip
    (local providers cost nothing; liveness reconciliation is their backstop)
    → policy max_wall_seconds → system default."""
    from domains.llm_providers.services.llm_executor import is_local_provider_kind
    from domains.run_policies.models import RunPolicy
    from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS

    override = int(
        ((run.usage or {}).get("workflow_overrides") or {}).get("max_wall_seconds") or 0
    )
    if override:
        return override
    if is_local_provider_kind(getattr(provider, "kind", "") or ""):
        return None
    if run.run_policy_uid:
        policy = await RunPolicy.nodes.get_or_none(uid=run.run_policy_uid)
        if policy is not None and policy.max_wall_seconds:
            return int(policy.max_wall_seconds)
    return DEFAULT_MAX_WALL_SECONDS


# ── DTO conversion ───────────────────────────────────────────────────────────


def run_to_dto(run: Run):
    from domains.investigations.schemas import (
        ExecutionMode,
        Executor,
        Playbook,
        RunDTO,
        RunTrigger,
    )

    def _enum_or(enum_cls, value, default):
        try:
            return enum_cls(value or default.value)
        except ValueError:
            return default

    return RunDTO(
        uid=run.uid,
        repository_uid=run.repository_uid,
        playbook=_enum_or(Playbook, run.playbook, Playbook.ASK),
        title=run.title or "",
        investigation_uid=run.investigation_uid or "",
        executor=_enum_or(Executor, run.executor, Executor.INTERNAL_LLM),
        execution_mode=_enum_or(ExecutionMode, run.execution_mode, ExecutionMode.ANALYZE_ONLY),
        run_policy_uid=run.run_policy_uid,
        status=_enum_or(RunStatus, run.status, RunStatus.QUEUED),
        linked_pr_uid=run.linked_pr_uid or "",
        linked_ticket_uid=run.linked_ticket_uid or "",
        linked_finding_uid=run.linked_finding_uid or "",
        target=dict(run.target or {}),
        sandbox_uid=run.sandbox_uid or "",
        workspace_spec=dict(run.workspace_spec or {}),
        cli_session_id=run.cli_session_id or "",
        turns=int(run.turns or 0),
        usage=dict(run.usage or {}),
        provider_uid=run.provider_uid or None,
        provider_label=(run.usage or {}).get("provider_label") or "",
        provider_kind=(run.usage or {}).get("provider_kind") or "",
        provider_model=(run.usage or {}).get("provider_model") or "",
        overlay_uid=getattr(run, "overlay_uid", "") or "",
        overlay_rev=int(getattr(run, "overlay_rev", 0) or 0),
        summary=dict(run.summary or {}),
        output_refs=list(run.output_refs or []),
        raw_artifact_uri=run.raw_artifact_uri or "",
        parse_status=run.parse_status or "ok",
        trigger=_enum_or(RunTrigger, run.trigger, RunTrigger.MANUAL),
        triggered_by=run.triggered_by or "",
        surface=run.surface or "runs",
        error=run.error or "",
        started_at=run.started_at,
        completed_at=run.completed_at,
        last_activity_at=run.last_activity_at,
        ended_at=run.ended_at,
        duration_ms=int(run.duration_ms or 0),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
