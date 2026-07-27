"""CLI executor adapters for Codex and OpenCode.

These adapters invoke the active CLI provider and parse a final JSON envelope
of platform-tool calls.

Codex is read/report only. OpenCode also takes WRITE runs (IMPLEMENT mode:
edit, test and `git commit` inside the sandbox clone) — but only when the model
behind it is the user's own local server, which `_may_write` re-checks here on
top of the lifecycle's dispatch-time gate. The agent's write surface still
stops at a local commit: `delivery.write_gate` validates and pushes.

Shared plumbing (provider/ceiling resolution, stream recording, envelope
extraction + tool dispatch, warnings-only ceiling accounting) lives in `_shared.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import HTTPException

from domains.areas.services import area_coverage
from domains.executors._shared import (
    StreamRecorder,
    _completed_via_mcp,
    ceiling_warnings,
    execute_envelope_tool_calls,
    extract_envelope,
    record_input,
    resolve_provider,
    resolve_wall_ceiling,
)
from domains.executors.base import AdapterRegistry, DispatchRequest, DispatchResult, ExecutorAdapter
from domains.executors.prompt_kit import stance_block, system_prompt as build_system_prompt
from domains.executors.quota import detect_quota_exhaustion
from domains.executors.reasoning import reasoning_args
from domains.runs.schemas import ExecutionMode, Executor, RunStatus
from domains.runs.services.run_events import append_event, preview, preview_structured
from domains.llm_providers.services import codex_cli
from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services import codex_credential
from domains.llm_providers.services.llm_executor import (
    OpenCodeEventReducer,
    invoke as invoke_provider,
    is_local_provider_kind,
    resolved_provider_kind,
)
from domains.platform_tools.complete_run import extract_outcome
from domains.run_policies.services.ceilings import UsageSnapshot
from infrastructure import artifact_store
from infrastructure.code_graph import CODE_GRAPH_PROMPT, code_graph_available

logger = logging.getLogger(__name__)

# Patch tools stay off on the tracking path. This denies a platform tool that
# would ask OpenSweep to APPLY a diff on the agent's behalf — orthogonal to a
# write run, where the agent edits and commits in its own sandbox clone.
_DENY_TOOLS = {"attach_patch_to_finding": "patch tools are disabled in tracking-only v1"}

# Transcript-tail continuation pass (Task 7).
# `codex exec` has no --resume/session handle, so its continuation technique is
# to re-prompt with a capped tail of the prior transcript as context (same
# approach as turn_cli.build_codex_prompt).
#
# opencode NO LONGER USES THIS. `opencode run -s <session id>` restores the real
# conversation across process boundaries (verified against 1.15.10: two separate
# processes, the second answering a question that only the first's context could
# answer), and the session id rides on every `--format json` event. The tail
# re-prompt survives for opencode only as the fallback for a pass that produced
# no session id at all.
CONTINUATION_TAIL_CAP = 8_000
# Back-compat alias: the cap was codex-only when it was introduced.
CODEX_CONTINUATION_TAIL_CAP = CONTINUATION_TAIL_CAP
_MIN_CONTINUATION_WALL_SECONDS = 120

_CONTINUATION_NUDGE_TRACKING = (
    "Continue the run — it is not finished. Work through the remaining scope, "
    "then emit the final JSON envelope of platform tool calls INCLUDING a "
    "complete_run entry with your end-of-run report."
)

# Write runs get a different nudge: for a read run "not finished" means scope
# left uninspected, but for an implement/fix run the expensive failure is an
# agent that edited files and stopped without committing — the sandbox is
# thrown away and the whole round produces nothing. Name that explicitly.
_CONTINUATION_NUDGE_WRITE = (
    "Continue the run — it is not finished. Finish the change described in the "
    "intent, run the relevant tests, and COMMIT your work inside this working "
    "copy (uncommitted edits are discarded with the sandbox — a local commit is "
    "how the platform picks the change up; still never push). Then emit the "
    "final JSON envelope INCLUDING a complete_run entry whose summary lists the "
    "commits you made and the test results."
)

# Area-map partition gate: a mapping run that left an axis unpartitioned is
# NOT finished, whatever its complete_run claims. Reuses the continuation
# machinery above — only the nudge differs, because the agent needs to be told
# WHICH axis and WHICH paths it skipped.
_CONTINUATION_NUDGE_AREA_GAP = (
    "The area map is NOT finished — you called complete_run (or stopped) with "
    "an axis left unpartitioned:\n\n{gaps}\n\n"
    "Both axes must partition the repository independently: every tracked file "
    "belongs to exactly one subsystem-axis leaf (kind subsystem, or ignore for "
    "non-auditable paths) AND exactly one feature-axis leaf (kind feature, or "
    "feature_ignore for paths that implement no product feature). Propose the "
    "missing areas with propose_area_edit until nothing is left over, then emit "
    "the final JSON envelope INCLUDING a complete_run entry."
)

# What makes a run a MAPPING run, i.e. one this gate may hold to a whole-repo
# partition. Two signals, either sufficient:
#   1. the run's agent is the "map-areas" playbook base — the authoritative
#      one, and the only signal an MCP-transport run gives (its proposals are
#      executed live, so the final envelope may hold nothing but complete_run);
#   2. the envelope proposes at least this many areas — a run reshaping the
#      partition wholesale, whatever it was dispatched as.
# The threshold is where the line sits: one or two propose_area_edit calls is
# an incidental fix ("this scope moved"), and demanding a full partition from
# such a run would hijack it.
_MAPPING_RUN_MIN_PROPOSALS = 3
_MAP_AREAS_AGENT_KEY = "map-areas"
# git ls-files over a workspace clone; a stuck git must never hold a run.
_TRACKED_FILES_TIMEOUT_SECONDS = 30


def envelope_has_complete_run(envelope: dict[str, Any] | None) -> bool:
    """True when a parsed final envelope contains a `complete_run` tool call.

    Envelope-based codex runs stamp Run.completed_at only AFTER the
    continuation decision (execute_envelope_tool_calls runs later), so
    `_completed_via_mcp` cannot see an envelope-path completion in time — the
    envelope itself is the authoritative first-pass completion signal.
    """
    if not envelope:
        return False
    for call in envelope.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("tool") == "complete_run":
            return True
    return False


def envelope_area_proposals(envelope: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The `propose_area_edit` args in a final envelope.

    Envelope tool calls have NOT been executed at the continuation gate, so
    these proposals exist nowhere else yet — coverage has to read them here
    or it would measure the map without the run's own work."""
    out: list[dict[str, Any]] = []
    for call in (envelope or {}).get("tool_calls") or []:
        if isinstance(call, dict) and call.get("tool") == "propose_area_edit":
            args = call.get("args")
            out.append(args if isinstance(args, dict) else {})
    return out


async def _run_agent_key(req: DispatchRequest) -> str:
    """The system-agent key behind this run ("" for user agents / no agent).

    map-areas runs are dispatched by the sweep flow with no ScheduledAgent, so
    the Run's own composed agent is the fallback lookup."""
    from domains.agents.models import Agent, ScheduledAgent
    from domains.agents.services.registry import agent_key
    from domains.runs.models import Run

    agent_uid = ""
    if req.scheduled_agent_uid:
        sa = await ScheduledAgent.nodes.get_or_none(uid=req.scheduled_agent_uid)
        agent_uid = (getattr(sa, "agent_uid", "") or "") if sa else ""
    if not agent_uid:
        run = await Run.nodes.get_or_none(uid=req.run_uid)
        agent_uid = (getattr(run, "agent_uid", "") or "") if run else ""
    if not agent_uid:
        return ""
    agent = await Agent.nodes.get_or_none(uid=agent_uid)
    return agent_key(getattr(agent, "source_url", "") or "") if agent else ""


async def _tracked_files(workspace_path: str) -> list[str]:
    """Tracked paths in the run's workspace clone, via the runs domain's git
    helper (one implementation of "run read-only git here")."""
    from domains.runs.services.run_changes import _git

    out = await asyncio.wait_for(
        _git(workspace_path, "ls-files"), timeout=_TRACKED_FILES_TIMEOUT_SECONDS
    )
    return [line for line in out.splitlines() if line]


async def _areas_after_run(
    req: DispatchRequest, proposals: list[dict[str, Any]]
) -> list[dict]:
    """The area map as this run would leave it: accepted areas, overwritten by
    the run's pending proposals (MCP transport) and by the proposals still
    sitting in its envelope (envelope transport), keyed by area key."""
    from domains.areas.models import Area, AreaEdit
    from domains.areas.services.area_service import normalize_key

    rows: dict[str, dict] = {}
    for a in await Area.nodes.filter(repository_uid=req.repository_uid):
        rows[a.key] = {
            "key": a.key,
            "kind": a.kind or "subsystem",
            "scope_paths": list(a.scope_paths or []),
            "enabled": bool(a.enabled),
        }
    for e in await AreaEdit.nodes.filter(
        repository_uid=req.repository_uid, status="pending", source_run_uid=req.run_uid
    ):
        rows[e.key or ""] = {
            "key": e.key or "",
            "kind": e.kind or "subsystem",
            "scope_paths": list(e.scope_paths or []),
            "enabled": bool(getattr(e, "proposed_enabled", True)),
        }
    for args in proposals:
        key = normalize_key(str(args.get("key") or ""))
        if not key:
            continue
        rows[key] = {
            "key": key,
            "kind": str(args.get("kind") or "subsystem"),
            "scope_paths": [str(p) for p in (args.get("scope_paths") or [])],
            "enabled": bool(args.get("enabled", True)),
        }
    return list(rows.values())


async def area_partition_nudge(
    req: DispatchRequest, envelope: dict[str, Any] | None
) -> str:
    """The continuation nudge for a mapping run that left an axis with gaps;
    "" when this is not a mapping run, both axes are whole, or coverage
    cannot be computed.

    Every failure mode degrades to "": a missing workspace, a git that errors
    or hangs, an unreachable graph. Nothing here is worth failing a finished
    run over — the gap is then reported by the areas UI instead."""
    try:
        proposals = envelope_area_proposals(envelope)
        if len(proposals) < _MAPPING_RUN_MIN_PROPOSALS:
            if await _run_agent_key(req) != _MAP_AREAS_AGENT_KEY:
                return ""
        workspace = req.repository_local_path or ""
        if not workspace:
            return ""
        tracked = await _tracked_files(workspace)
        if not tracked:
            return ""  # no tree, no judgement
        coverage = area_coverage.axis_coverage(
            tracked, await _areas_after_run(req, proposals)
        )
        gaps = [
            "- " + area_coverage.gap_sentence(axis, coverage[axis])
            for axis in area_coverage.incomplete_axes(coverage)
        ]
        if not gaps:
            return ""
        return _CONTINUATION_NUDGE_AREA_GAP.format(gaps="\n".join(gaps))
    except Exception as exc:  # noqa: BLE001 — advisory gate, never run-fatal
        logger.warning(
            f"run {req.run_uid}: area partition gate skipped "
            f"({type(exc).__name__}: {exc})",
            extra={"tag": "areas"},
        )
        return ""


def continuation_prompt(nudge: str, transcript_tail: str) -> str:
    """`codex exec` has no --resume: re-prompt with a capped tail of the prior
    transcript as context (same technique as turn_cli.build_codex_prompt).

    Note what this technique does NOT restore: the CLI's own session state.
    The second pass is a fresh process that has read only the tail, so it
    re-derives its picture of the repo from scratch. For a WRITE run the
    sandbox working copy is the thing that carries state across the boundary —
    which is exactly why the write nudge insists on committing.

    opencode reaches this only when its session id was NOT captured (see
    `_continuation_payload`); with a session id it gets the real thing,
    `-s <id>`, and the nudge alone.
    """
    tail = transcript_tail[-CONTINUATION_TAIL_CAP:]
    return (
        "Your previous attempt at this task stopped early (context below — "
        "this CLI has no session resume):\n"
        f"{tail}\n\n{nudge}"
    )


# Historical name — imported by tests and by the codex-era call sites.
codex_continuation_prompt = continuation_prompt


def _continuation_payload(
    *, nudge: str, transcript_tail: str, session_id: str
) -> tuple[str, str]:
    """(prompt, session id) for the continuation pass.

    With a CLI session handle the nudge stands alone — the CLI reloads the real
    conversation, so pasting a transcript tail back in would only re-spend
    context on something the model already has. Without one, fall back to the
    tail re-prompt and a fresh session; a missing session id must degrade the
    continuation, never crash the run.
    """
    if session_id:
        return nudge, session_id
    return continuation_prompt(nudge, transcript_tail), ""


# The codex `exec --json` stream reducer lives in the shared codex adapter;
# re-exported here under its historical name for existing call sites/tests.
_codex_delta_feeder = codex_cli.delta_feeder


async def _persist_opencode_session(run_uid: str, session_id: str) -> None:
    """Record the opencode session handle on the Run.

    Reuses `Run.cli_session_id`, the field claude_code already fills for
    `--resume`. Both are "the CLI session this run owns", and every consumer of
    the field gates on the executor first (`handoff.handoff_mode`,
    `turn_service._SUBPROCESS_EXECUTORS`), so an opencode id stored here can
    never be handed to `claude --resume`.

    Best-effort: a graph write that fails must not fail a finished run — the
    only thing lost is the continuation pass's session resume, which already
    has a fallback.
    """
    if not run_uid or not session_id:
        return
    from domains.runs.models import Run

    try:
        run = await Run.nodes.get_or_none(uid=run_uid)
        if run is not None and (run.cli_session_id or "") != session_id:
            run.cli_session_id = session_id
            await run.save()
    except Exception as exc:  # noqa: BLE001 — telemetry, never run-fatal
        logger.warning(
            f"run {run_uid}: persisting opencode session id failed "
            f"({type(exc).__name__}: {exc})",
            extra={"tag": "opencode"},
        )


def _opencode_usage(inv) -> dict[str, Any]:
    """The `extra["opencode"]` block an invocation carries, or {}.

    Always optional. opencode bug #26855 lets `--format json` exit BEFORE the
    final `step_finish`, so tokens/cost are missing from otherwise perfectly
    successful runs — which is why nothing downstream may treat their absence
    as an error, and why process exit (not `step_finish`) is the completion
    signal everywhere in this adapter.
    """
    extra = getattr(inv, "extra", None)
    block = extra.get("opencode") if isinstance(extra, dict) else None
    return block if isinstance(block, dict) else {}


def _merge_opencode_usage(into: dict[str, Any], inv) -> dict[str, Any]:
    """Accumulate one pass's opencode telemetry into the run total.

    Summed across passes for the same reason it is summed across steps: each
    pass is a separate set of billed API calls, so the run's cost is their sum.
    The session id is NOT summed — both passes report the same one.
    """
    block = _opencode_usage(inv)
    if not block:
        return into
    # `tokens` is seeded even when the pass reported none, so the usage payload
    # has one shape whether or not #26855 ate the final `step_finish` — a
    # consumer that has to branch on a missing key is a consumer that will
    # eventually forget to.
    into.setdefault("tokens", {})
    into.setdefault("cost", 0.0)
    for key, value in (block.get("tokens") or {}).items():
        if isinstance(value, (int, float)):
            into["tokens"][key] = into["tokens"].get(key, 0) + int(value)
    if isinstance(block.get("cost"), (int, float)):
        into["cost"] = round(into.get("cost", 0.0) + float(block["cost"]), 6)
    into["steps"] = into.get("steps", 0) + int(block.get("steps") or 0)
    if block.get("session_id"):
        into["session_id"] = block["session_id"]
    return into


def _append_stream_event(run_uid: str, event: dict[str, Any]) -> None:
    """Write one reducer event to the run transcript, truncated for the UI.

    Truncation happens HERE rather than in the reducer because the reducer also
    feeds envelope extraction and the raw artifact, which must see the full
    text. `preview_structured` keeps a tool input JSON-parseable after cutting
    so the UI can still render file edits as diffs (same treatment claude_code
    gives its tool_use blocks)."""
    event = dict(event)
    etype = str(event.pop("type", ""))
    if not etype:
        return
    if "input" in event:
        event["input"] = preview_structured(event["input"])
    if "output" in event:
        event["output"] = preview(event["output"])
    append_event(run_uid, etype, **event)


def _raw_event_stream(inv) -> str:
    """The raw JSONL opencode emitted, or the plain transcript for every other
    transport. This is what lands in the `raw_transcript` artifact: when the
    reducer misreads an event, the artifact is the only place left holding the
    bytes it misread."""
    extra = getattr(inv, "extra", None)
    if isinstance(extra, dict) and extra.get("opencode_raw_events"):
        return str(extra["opencode_raw_events"])
    return inv.raw_output or ""


class _CLITrackingAdapter(ExecutorAdapter):
    provider_kind: str
    name: Executor
    # Whether this CLI may take IMPLEMENT runs at all, before the per-provider
    # locality check in `_may_write`. False for codex: `exec --json` is a
    # one-shot tracking transport and no write playbook targets it.
    local_write_capable: bool = False

    def _may_write(self, provider: LLMProvider) -> bool:
        """Whether this adapter may run an IMPLEMENT run on this provider.

        Deliberately duplicates the lifecycle's `_provider_supports_write`
        decision instead of trusting it. The lifecycle gate runs when the Run
        row is created; a quota pause can leave a write run queued for hours,
        and a provider row is editable the whole time. Re-checking at the
        moment we are about to hand an agent a write sandbox is the difference
        between "the operator repointed opencode at a cloud endpoint" being a
        config change and being an unattended cloud write run.
        """
        return self.local_write_capable and is_local_provider_kind(
            resolved_provider_kind(provider)
        )

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        provider = await resolve_provider(
            req.provider_uid, kind=self.provider_kind, repository_uid=req.repository_uid
        )
        if provider is None:
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"active provider is not kind={self.provider_kind}",
                summary=f"{self.name.value} requires the active provider to be {self.provider_kind}",
            )

        if req.mode == ExecutionMode.IMPLEMENT and not self._may_write(provider):
            # Fail loudly rather than quietly running the read-only prompt: a
            # write run that returns findings instead of commits looks like the
            # agent chose not to make the change, and the caller (fix rounds,
            # ticket implementation) counts the round as spent either way.
            detail = (
                f"{self.name.value} is not write-capable"
                if not self.local_write_capable
                else (
                    f"{self.name.value} takes write runs only on a local model — "
                    f"provider {provider.label!r} resolves to "
                    f"{resolved_provider_kind(provider) or 'a non-local endpoint'}"
                )
            )
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"execution_mode=implement refused: {detail}",
                summary=f"{self.name.value} refused a write run: {detail}",
            )

        if req.model_override:
            # In-memory only — per-stage workflow override, never saved.
            provider.model = req.model_override

        # App-server path (opt-in): a TRANSPORT swap only — the run still goes
        # through _run_passes, so envelope tool_calls, the continuation pass and
        # quota handling all apply. The persistent session holds the credential
        # lease for ITS lifetime, so this run takes no per-run lease; that is what
        # lets many runs share one server, each on its own codex thread.
        if self.provider_kind == "codex_subscription" and codex_cli.app_server_enabled(provider):
            try:
                session = await codex_cli.acquire_app_server(provider)
            except HTTPException as exc:
                return self._paused_busy(req, exc)
            try:
                return await self._run_passes(req, provider, started, session=session)
            finally:
                await codex_cli.release_app_server(session)

        # Codex subscriptions serialize per credential and durably persist any
        # rotation codex performs across the run's passes (inert for opencode and
        # for bind-mount codex — see codex_credential.codex_credential_txn). Held
        # across ALL passes so the lease covers the continuation and the rotated
        # auth.json is written back once, on exit. Without it a run seeds the
        # sealed auth.json, lets codex rotate the refresh token, then discards it,
        # so the next run reuses a consumed refresh token and codex fails with
        # "access token could not be refreshed".
        try:
            async with codex_credential.codex_credential_txn(provider):
                return await self._run_passes(req, provider, started)
        except HTTPException as exc:
            return self._paused_busy(req, exc)

    def _paused_busy(self, req: DispatchRequest, exc: HTTPException) -> DispatchResult:
        """Another codex run holds this subscription's exclusive lease past the
        wait budget. Treat it like a quota pause (a state, not a failure):
        PAUSED_QUOTA is resumable, so the run is re-dispatched later instead of
        failing hard — mirrors the turn path returning a retryable 503."""
        logger.info(
            f"{self.name.value} run {req.run_uid}: codex subscription busy "
            f"({getattr(exc, 'detail', exc)}) — pausing for retry",
            extra={"tag": "codex"},
        )
        return DispatchResult(
            status=RunStatus.PAUSED_QUOTA,
            error="codex subscription busy — another run holds the credential lease",
            summary=f"{self.name.value} paused: codex subscription busy — will retry",
        )

    async def _run_passes(
        self, req: DispatchRequest, provider: LLMProvider, started: float, session=None
    ) -> DispatchResult:
        """The run pipeline. `session` swaps the codex TRANSPORT to the persistent
        app-server; everything else — envelope extraction, executing the agent's
        tool_calls, the continuation pass, quota handling, the raw transcript —
        is identical, because that is what makes a run a run."""
        timeout = resolve_wall_ceiling(req, provider.kind)
        # `dispatch` already refused an IMPLEMENT run this adapter/provider pair
        # may not take, so reaching here in IMPLEMENT mode means write is
        # authorised — the write prompt and the write instruction go together.
        write_run = req.mode == ExecutionMode.IMPLEMENT
        instruction = _instruction(req, timeout, write_run=write_run)
        # Both CLIs get the code-graph MCP server over the workspace clone —
        # opencode through its generated config, codex through `-c` argv
        # overrides (llm_executor) — under this same availability gate.
        system_prompt = _SYSTEM_PROMPT_WRITE if write_run else _SYSTEM_PROMPT
        if code_graph_available(req.repository_local_path or ""):
            system_prompt = system_prompt + "\n" + CODE_GRAPH_PROMPT
        await record_input(
            req.run_uid,
            system_prompt=system_prompt,
            instruction=instruction,
        )
        append_event(req.run_uid, "user_message", text=instruction)

        # on_chunk delivers the running TOTAL per stream; the transcript wants
        # only the new tail, as assistant_text chunks (consecutive chunks merge
        # in the UI). BOTH CLIs now stream structured JSONL — codex via
        # `exec --json` (agent_message text only, not the raw envelope/reasoning
        # noise) and opencode via `--format json` (assistant text plus real
        # tool_use/tool_result cards, which opencode runs never had before).
        is_codex = self.provider_kind == "codex_subscription"
        is_opencode = self.provider_kind == "opencode"
        # The app-server streams plain agent text, not `exec --json` events, so
        # it needs the raw-tail passthrough rather than the JSONL feeder.
        use_app_server = session is not None
        # NB: the codex feeder and the opencode reducer are built PER PASS
        # inside `_stream_pump`, not here — both are stateful (consumed
        # offsets, partial-line buffers) and each pass is a separate process
        # streaming from byte zero, so a shared one would read the second
        # pass's first chunk as a rewind.

        async def _invoke(*, instruction: str, timeout_seconds, on_chunk, cli_session_id=""):
            if use_app_server:
                return await codex_cli.invoke_via_app_server(
                    session, provider, system_prompt=system_prompt, instruction=instruction,
                    timeout_seconds=timeout_seconds, working_dir=req.repository_local_path,
                    on_chunk=on_chunk, run_uid=req.run_uid,
                )
            return await invoke_provider(
                provider, system_prompt=system_prompt, instruction=instruction,
                timeout_seconds=timeout_seconds, working_dir=req.repository_local_path,
                on_chunk=on_chunk, run_uid=req.run_uid, extra_cli_args=reasoning_cli_args,
                cli_session_id=cli_session_id,
            )

        def _stream_pump(label: str = f"live {self.name.value} transcript"):
            """A per-pass `on_chunk` + its StreamRecorder.

            Built per pass because every reducer here is stateful (consumed
            offsets, partial-line buffers, text-part bookkeeping) and the
            continuation pass is a different process with its own stream from
            byte zero — sharing one would make the second pass's first chunk
            look like a rewind."""
            recorder = StreamRecorder(
                run_uid=req.run_uid,
                repository_uid=req.repository_uid,
                label=label,
            )
            feed = _codex_delta_feeder() if (is_codex and not use_app_server) else None
            reducer = OpenCodeEventReducer() if is_opencode else None
            streamed_len = {"stdout": 0}

            async def _on_chunk(stream: str, text: str) -> None:
                if stream == "stdout":
                    if reducer is not None:
                        for event in reducer.feed_total(text):
                            _append_stream_event(req.run_uid, event)
                    elif feed is not None:
                        for delta in feed(text):
                            append_event(req.run_uid, "assistant_text", text=delta)
                    else:
                        delta = text[streamed_len["stdout"]:]
                        if delta:
                            streamed_len["stdout"] = len(text)
                            append_event(req.run_uid, "assistant_text", text=delta)
                await recorder.record_total(stream, text)

            async def _close() -> None:
                # The last opencode event is not guaranteed to be newline
                # terminated; flush so its transcript card is not lost.
                if reducer is not None:
                    for event in reducer.flush():
                        _append_stream_event(req.run_uid, event)
                await recorder.close()

            return _on_chunk, _close

        # Reasoning level → codex `-c model_reasoning_effort=…` argv override
        # (empty for opencode and for unset levels).
        reasoning_cli_args = reasoning_args(req.reasoning, provider.kind).get("cli_config") or []

        _on_chunk, _close_stream = _stream_pump()
        try:
            inv = await _invoke(
                instruction=instruction, timeout_seconds=timeout, on_chunk=_on_chunk,
            )
        finally:
            await _close_stream()
        wall = time.monotonic() - started

        # Accumulate raw output across passes; start with the first pass.
        # For opencode `raw_output` is the transcript the reducer rebuilt from
        # the event stream (see llm_executor.apply_opencode_events) — the
        # envelope, the quota tail and the tail re-prompt all live in THAT, not
        # in the JSONL, which is kept separately for the debug artifact.
        raw_stdout = inv.raw_output or ""
        raw_stderr = inv.stderr or ""
        raw_events = _raw_event_stream(inv)

        # opencode session continuity: the handle rides on every `--format json`
        # event, so it is captured even from a pass that crashed after one line.
        # Persisted before the continuation decision so the run stays resumable
        # even if this process dies between the passes.
        cli_usage: dict[str, Any] = _merge_opencode_usage({}, inv)
        opencode_session_id = str(cli_usage.get("session_id") or "")
        if opencode_session_id:
            await _persist_opencode_session(req.run_uid, opencode_session_id)

        envelope = extract_envelope(raw_stdout)
        parse_status = "ok" if envelope else "degraded"

        # Quota is a state, not a failure (§8): pause instead of failing when
        # the CLI died on a usage/rate limit. A completed tool flow (a parsed
        # envelope) is agent SUCCESS and is never treated as quota.
        if envelope is None and detect_quota_exhaustion(
            inv.exit_code, raw_stdout, raw_stderr
        ):
            raw_uri = artifact_store.put(
                repository_uid=req.repository_uid,
                run_uid=req.run_uid,
                content=raw_events + ("\n--- STDERR ---\n" + raw_stderr if raw_stderr else ""),
                artifact_type="raw_transcript",
                extension="txt",
                summary=f"{self.name.value} raw transcript",
            )
            return DispatchResult(
                status=RunStatus.PAUSED_QUOTA,
                raw_artifact_uri=raw_uri,
                parse_status=parse_status,
                usage={
                    "wall_seconds": round(wall, 2),
                    "exit_code": inv.exit_code,
                    "provider_kind": provider.kind,
                    "transport": inv.transport,
                    # A quota-paused run still spent whatever it spent, and it
                    # still owns a session the retry can resume.
                    **({"cli_usage": cli_usage} if cli_usage else {}),
                },
                error="provider quota/rate limit reached",
                summary=f"{self.name.value} paused: provider quota exhausted — will retry",
            )

        # Continuation pass (Task 7): if the CLI did not finish and wall budget
        # remains, re-prompt once with a capped tail of the prior transcript as
        # context. "Did not finish" has two signals: the MCP-path completion
        # (`_completed_via_mcp`) AND the envelope-path completion
        # (`complete_run` in the first-pass envelope, already parsed above) —
        # the latter is required because envelope tool calls execute later, so
        # completed_at is not yet stamped at this gate.
        # A third signal OVERRIDES both: an area-mapping run whose map still has
        # an unpartitioned axis has not finished the job it was given, so
        # `area_partition_nudge` forces the pass even on a clean complete_run.
        # Both CLIs get this, by different means: opencode resumes its real
        # session with `-s <id>` (as good as claude_code's `--resume`), while
        # codex, which has no session handle, still gets a fresh process seeded
        # with the transcript tail. Either way it is the difference between one
        # shot and two for an agent that stopped at 80%. It matters most for
        # WRITE runs, where pass one may have edited files without committing.
        # Tracking variable: last_inv points at whichever pass ran last so that
        # status decisions (wall-kill, FAILED) and usage always reflect the
        # final pass outcome.
        last_inv = inv
        continuation_pass = False
        continuation_reason = ""
        remaining_wall = (timeout - wall) if timeout is not None else None
        # Policy gate: max_continuation_passes=0 disables the (single)
        # continuation; None/>=1 allows it.
        policy_passes = (
            req.policy.max_continuation_passes if req.policy is not None else None
        )
        nudge = ""
        if (
            inv.ok  # gate: a crashed/timed-out first pass must NOT be re-prompted
            and (policy_passes is None or int(policy_passes) >= 1)
            and (remaining_wall is None or remaining_wall > _MIN_CONTINUATION_WALL_SECONDS)
        ):
            finished = envelope_has_complete_run(envelope) or await _completed_via_mcp(
                req.run_uid
            )
            # A mapping run that left an axis unpartitioned is not finished
            # even when it says it is — that exact claim is how a repo ended
            # up with a complete subsystem axis and 5 feature areas. The gap
            # nudge also outranks the generic one when the run stopped early:
            # naming the missing paths beats "keep going". Bounded by the
            # same single-continuation policy as every other pass.
            gap_nudge = await area_partition_nudge(req, envelope)
            if gap_nudge:
                nudge = gap_nudge
                continuation_reason = "area_partition_gap"
            elif not finished:
                nudge = (
                    _CONTINUATION_NUDGE_WRITE if write_run
                    else _CONTINUATION_NUDGE_TRACKING
                )
                continuation_reason = "incomplete_run"
        if nudge:
            if continuation_reason == "area_partition_gap":
                logger.info(
                    f"run {req.run_uid}: area map incomplete — forcing a "
                    "continuation pass to finish the partition",
                    extra={"tag": "areas"},
                )
            # opencode resumes the real session and needs only the nudge; codex
            # (and an opencode pass that yielded no session id) still gets the
            # transcript tail.
            cont_prompt, cont_session_id = _continuation_payload(
                nudge=nudge,
                transcript_tail=raw_stdout,
                session_id=opencode_session_id if is_opencode else "",
            )
            append_event(req.run_uid, "user_message", text=nudge)

            # Same transport and parsing as the first pass, on a fresh set of
            # stateful reducers (see `_stream_pump`).
            _on_cont_chunk, _close_cont_stream = _stream_pump(
                f"live {self.name.value} continuation transcript"
            )
            try:
                cont_inv = await _invoke(
                    instruction=cont_prompt,
                    timeout_seconds=int(remaining_wall) if remaining_wall is not None else None,
                    on_chunk=_on_cont_chunk,
                    cli_session_id=cont_session_id,
                )
            finally:
                await _close_cont_stream()

            last_inv = cont_inv  # status decisions now reflect the continuation pass

            cont_stdout = cont_inv.raw_output or ""
            raw_stdout = raw_stdout + "\n\n--- CONTINUATION PASS ---\n" + cont_stdout
            raw_events = (
                raw_events + "\n\n--- CONTINUATION PASS ---\n" + _raw_event_stream(cont_inv)
            )
            cont_stderr = cont_inv.stderr or ""
            if cont_stderr:
                raw_stderr = raw_stderr + "\n--- CONTINUATION PASS STDERR ---\n" + cont_stderr
            cli_usage = _merge_opencode_usage(cli_usage, cont_inv)
            wall = time.monotonic() - started
            continuation_pass = True

            # Merge continuation envelope into first-pass envelope.
            cont_envelope = extract_envelope(cont_stdout)
            if cont_envelope is not None:
                first_calls = (envelope.get("tool_calls") or []) if envelope else []
                cont_calls = cont_envelope.get("tool_calls") or []
                merged_calls = first_calls + cont_calls
                # Count how many complete_run entries appear across both passes
                # to log when both contained one (continuation's wins per spec).
                complete_run_count = sum(
                    1
                    for c in merged_calls
                    if (c.get("tool") if isinstance(c, dict) else None) == "complete_run"
                )
                if complete_run_count >= 2:
                    logger.info(
                        "%s continuation: both passes emitted complete_run — "
                        "the continuation's wins (%d total in merged list)",
                        self.name.value,
                        complete_run_count,
                    )
                # Use the continuation envelope as the base (it has the later summary).
                envelope = dict(cont_envelope)
                envelope["tool_calls"] = merged_calls
                parse_status = "ok"
            elif envelope is None:
                # Neither pass produced a parseable envelope.
                parse_status = "degraded"

        raw_uri = artifact_store.put(
            repository_uid=req.repository_uid,
            run_uid=req.run_uid,
            content=raw_events + ("\n--- STDERR ---\n" + raw_stderr if raw_stderr else ""),
            artifact_type="raw_transcript",
            extension="txt",
            summary=f"{self.name.value} raw transcript",
        )

        tool_results: list[dict[str, Any]] = []
        output_refs: list[str] = [raw_uri]
        outcome: dict[str, Any] = {}
        if envelope:
            tool_results, refs, outcome = await execute_envelope_tool_calls(
                calls=envelope.get("tool_calls"),
                req=req,
                executor_value=self.name.value,
                deny_tools=_DENY_TOOLS,
            )
            output_refs.extend(refs)

        # Post-run ceiling accounting (Task 5): warnings only — a finished run
        # is never retroactively failed; LIMIT_EXCEEDED is reserved for runs a
        # limit actually stopped (wall kill surfaces as inv.error "timed out").
        # tokens/dollars are reported, not enforced (ceilings.check only gates
        # wall/turns/files), and they are BEST EFFORT: opencode bug #26855 lets
        # `--format json` exit before the final `step_finish`, so a perfectly
        # good run can land here with zero tokens. Nothing below may treat that
        # as a failure — process exit is the completion signal.
        usage_snapshot = UsageSnapshot(
            wall_seconds=wall,
            tool_turns=len(envelope.get("tool_calls", [])) if envelope else 0,
            tokens=int((cli_usage.get("tokens") or {}).get("total") or 0),
            dollars=float(cli_usage.get("cost") or 0.0),
        )
        warnings = ceiling_warnings(
            policy=req.policy, usage=usage_snapshot, wall_ceiling=timeout
        )

        # Use last_inv (= cont_inv when continuation ran, else inv) so that
        # wall-kill detection, FAILED status, and exit_code all reflect the
        # final pass.  first_pass_exit_code is included for observability when
        # a continuation was attempted.
        wall_killed = last_inv.error.startswith("timed out") if last_inv.error else False
        if wall_killed:
            status = RunStatus.LIMIT_EXCEEDED
        elif last_inv.error:
            status = RunStatus.FAILED
        else:
            status = RunStatus.AWAITING_INPUT
        usage: dict[str, Any] = {
            "wall_seconds": round(wall, 2),
            "exit_code": last_inv.exit_code,
            "provider_kind": provider.kind,
            "transport": last_inv.transport,
            "tool_calls": len(envelope.get("tool_calls", [])) if envelope else 0,
            "tool_results": tool_results,
            "warnings": warnings,
            "continuation_pass": continuation_pass,
        }
        if cli_usage:
            # Real provider telemetry for opencode runs, which the platform had
            # none of before: token counters and dollar cost summed over every
            # step of every pass, plus the session handle the run can be
            # resumed from. Absent (falsy) whenever the CLI gave us nothing —
            # see #26855.
            usage["cli_usage"] = cli_usage
        if continuation_pass:
            usage["first_pass_exit_code"] = inv.exit_code
            usage["continuation_reason"] = continuation_reason
        return DispatchResult(
            status=status,
            raw_artifact_uri=raw_uri,
            parse_status=parse_status,
            usage=usage,
            output_refs=output_refs,
            error=last_inv.error or "",
            summary=f"{self.name.value} finished in {wall:.1f}s",
            outcome=outcome or extract_outcome({"summary": (envelope or {}).get("summary")}),
        )

class CodexAdapter(_CLITrackingAdapter):
    name = Executor.CODEX
    provider_kind = "codex_subscription"


class OpenCodeAdapter(_CLITrackingAdapter):
    name = Executor.OPENCODE
    provider_kind = "opencode"
    # "Bring your own agent" extends to write runs — but only on a local
    # model. `_may_write` pairs this flag with the provider's resolved kind.
    local_write_capable = True


# Assembled by the prompt kit: shared core + the cli_tracking delta (tool
# list rendered from the registry, JSON envelope output contract, native
# opensweep_* MCP preference note).
_SYSTEM_PROMPT = build_system_prompt("cli_tracking")
# The IMPLEMENT-mode counterpart: same envelope contract, but the read-only
# rule is replaced by the write hard rules (edit + commit in the sandbox,
# never push — the platform validates and pushes).
_SYSTEM_PROMPT_WRITE = build_system_prompt("cli_tracking_write")


def _instruction(
    req: DispatchRequest, wall_ceiling: int | None = None, *, write_run: bool = False
) -> str:
    # The closing line is the one place the run's MODE shows up in the
    # instruction. Leaving "Investigate only." on a write run would contradict
    # the implement/fix intent the delivery services composed above it, and an
    # agent given both instructions reliably obeys the last, most specific one
    # — which is this trailer, not the intent.
    closing = (
        """Make the change described in the intent, run the relevant tests, and COMMIT
inside this working copy — never push. Then report through the final JSON
tool_calls envelope, ending with complete_run."""
        if write_run
        else """Investigate only. Record bugs, gaps, and improvements through the final
JSON tool_calls envelope; persist durable facts with write_memory."""
    )
    return f"""# Run

repository_uid: {req.repository_uid}
run_uid: {req.run_uid}

# Intent

{req.intent}

# Target

```json
{json.dumps(req.target or {}, indent=2)}
```

{req.context or ""}

{stance_block(req.policy, wall_ceiling, req.effort, write_run=write_run)}

{closing}
"""


AdapterRegistry.register(CodexAdapter())
AdapterRegistry.register(OpenCodeAdapter())
