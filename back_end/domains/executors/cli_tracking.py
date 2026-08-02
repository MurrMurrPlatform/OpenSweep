"""Tracking CLI executor adapter for OpenCode.

The adapter invokes the active opencode provider and parses a final JSON
envelope of platform-tool calls (read runs), or runs the write contract
(implement/fix runs — G1 local-LLM delivery loop).

Shared plumbing (provider/ceiling resolution, stream recording, envelope
extraction + tool dispatch, warnings-only ceiling accounting) lives in `_shared.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

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
from domains.runs.schemas import Executor, ExecutionMode, RunStatus
from domains.runs.services.run_events import append_event
from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services.llm_executor import (
    invoke as invoke_provider,
)
from domains.platform_tools.complete_run import extract_outcome
from domains.run_policies.services.ceilings import UsageSnapshot
from infrastructure import artifact_store
from infrastructure.code_graph import CODE_GRAPH_PROMPT, code_graph_available

logger = logging.getLogger(__name__)

# Patch tools stay off in tracking-only v1.
_DENY_TOOLS = {"attach_patch_to_finding": "patch tools are disabled in tracking-only v1"}

# Continuation pass (Task 7).
# `opencode run` has no session resume; the continuation technique re-prompts
# with a capped tail of the prior transcript (same approach as
# turn_cli.build_transcript_prompt).
CONTINUATION_TAIL_CAP = 8_000
_MIN_CONTINUATION_WALL_SECONDS = 120

_CONTINUATION_NUDGE_TRACKING = (
    "Continue the run — it is not finished. Work through the remaining scope, "
    "then emit the final JSON envelope of platform tool calls INCLUDING a "
    "complete_run entry with your end-of-run report."
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

    Envelope-based runs stamp Run.completed_at only AFTER the continuation
    decision (execute_envelope_tool_calls runs later), so `_completed_via_mcp`
    cannot see an envelope-path completion in time — the envelope itself is
    the authoritative first-pass completion signal.
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
    """opencode run has no session resume: re-prompt with a capped tail of the
    prior transcript as context (same technique as turn_cli.build_transcript_prompt)."""
    tail = transcript_tail[-CONTINUATION_TAIL_CAP:]
    return (
        "Your previous attempt at this task stopped early (context below — "
        "this CLI has no session resume):\n"
        f"{tail}\n\n{nudge}"
    )


class _CLITrackingAdapter(ExecutorAdapter):
    provider_kind: str
    name: Executor

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

        if req.model_override:
            # In-memory only — per-stage workflow override, never saved.
            provider.model = req.model_override

        return await self._run_passes(req, provider, started)

    async def _run_passes(
        self, req: DispatchRequest, provider: LLMProvider, started: float
    ) -> DispatchResult:
        """The run pipeline: envelope extraction, executing the agent's
        tool_calls, the continuation pass, quota handling, the raw transcript."""
        timeout = resolve_wall_ceiling(req, provider.kind)
        # Write mode (OpenCode local-LLM delivery loop, G1): an implement/fix run
        # on a write-capable tracking executor gets the write contract — edit +
        # test + COMMIT in the sandbox clone with the CLI's own tools — instead of
        # the read-only "investigate + emit envelope" one. The finalize path then
        # validates those commits and pushes, exactly as it does for claude_code.
        write_mode = (
            self.name == Executor.OPENCODE and req.mode == ExecutionMode.IMPLEMENT
        )
        base_prompt = _SYSTEM_PROMPT_WRITE if write_mode else _SYSTEM_PROMPT
        instruction = (
            _write_instruction(req, timeout) if write_mode else _instruction(req, timeout)
        )
        # The CLI gets the code-graph MCP server over the workspace clone —
        # through opencode's generated config (llm_executor) — under this same
        # availability gate.
        system_prompt = base_prompt
        if code_graph_available(req.repository_local_path or ""):
            system_prompt = base_prompt + "\n" + CODE_GRAPH_PROMPT
        await record_input(
            req.run_uid,
            system_prompt=system_prompt,
            instruction=instruction,
        )
        append_event(req.run_uid, "user_message", text=instruction)

        async def _invoke(*, instruction: str, timeout_seconds, on_chunk):
            return await invoke_provider(
                provider, system_prompt=system_prompt, instruction=instruction,
                timeout_seconds=timeout_seconds, working_dir=req.repository_local_path,
                on_chunk=on_chunk, run_uid=req.run_uid,
            )

        # on_chunk delivers the running TOTAL per stream; the transcript wants
        # only the new tail, as assistant_text chunks (consecutive chunks merge
        # in the UI). opencode streams plain text, which passes through as the
        # raw tail.
        streamed_len = {"stdout": 0}
        recorder = StreamRecorder(
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            label=f"live {self.name.value} transcript",
        )

        async def _on_chunk(stream: str, text: str) -> None:
            if stream == "stdout":
                delta = text[streamed_len["stdout"]:]
                if delta:
                    streamed_len["stdout"] = len(text)
                    append_event(req.run_uid, "assistant_text", text=delta)
            await recorder.record_total(stream, text)

        try:
            inv = await _invoke(
                instruction=instruction, timeout_seconds=timeout, on_chunk=_on_chunk,
            )
        finally:
            await recorder.close()
        wall = time.monotonic() - started

        # Accumulate raw output across passes; start with the first pass.
        raw_stdout = inv.raw_output or ""
        raw_stderr = inv.stderr or ""

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
                content=raw_stdout + ("\n--- STDERR ---\n" + raw_stderr if raw_stderr else ""),
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
                },
                error="provider quota/rate limit reached",
                summary=f"{self.name.value} paused: provider quota exhausted — will retry",
            )

        # Continuation pass (Task 7): if the agent did not finish and wall
        # budget remains, re-prompt once with a capped tail of the prior
        # transcript as context (`opencode run` has no session resume).
        # "Did not finish" has two signals: the MCP-path completion
        # (`_completed_via_mcp`, for MCP-configured runs) AND the envelope-path
        # completion (`complete_run` in the first-pass envelope, already parsed
        # above) — the latter is required because envelope tool calls execute
        # later, so completed_at is not yet stamped at this gate.
        # A third signal OVERRIDES both: an area-mapping run whose map still has
        # an unpartitioned axis has not finished the job it was given, so
        # `area_partition_nudge` forces the pass even on a clean complete_run.
        # Write runs are excluded — the nudges speak the envelope contract,
        # which write runs don't use.
        # Tracking variable: last_inv points at whichever pass ran last so that
        # status decisions (wall-kill, FAILED) and usage always reflect the
        # final pass outcome.
        last_inv = inv
        continuation_pass = False
        continuation_reason = ""
        if not write_mode:
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
                    nudge = _CONTINUATION_NUDGE_TRACKING
                    continuation_reason = "incomplete_run"
            if nudge:
                if continuation_reason == "area_partition_gap":
                    logger.info(
                        f"run {req.run_uid}: area map incomplete — forcing a "
                        "continuation pass to finish the partition",
                        extra={"tag": "areas"},
                    )
                cont_prompt = continuation_prompt(nudge, raw_stdout)
                append_event(req.run_uid, "user_message", text=nudge)

                cont_recorder = StreamRecorder(
                    run_uid=req.run_uid,
                    repository_uid=req.repository_uid,
                    label=f"live {self.name.value} continuation transcript",
                )
                cont_streamed = {"stdout": 0}

                async def _on_cont_chunk(stream: str, text: str) -> None:
                    if stream == "stdout":
                        delta = text[cont_streamed["stdout"]:]
                        if delta:
                            cont_streamed["stdout"] = len(text)
                            append_event(req.run_uid, "assistant_text", text=delta)
                    await cont_recorder.record_total(stream, text)

                try:
                    cont_inv = await _invoke(
                        instruction=cont_prompt,
                        timeout_seconds=int(remaining_wall) if remaining_wall is not None else None,
                        on_chunk=_on_cont_chunk,
                    )
                finally:
                    await cont_recorder.close()

                last_inv = cont_inv  # status decisions now reflect the continuation pass

                cont_stdout = cont_inv.raw_output or ""
                raw_stdout = raw_stdout + "\n\n--- CONTINUATION PASS ---\n" + cont_stdout
                cont_stderr = cont_inv.stderr or ""
                if cont_stderr:
                    raw_stderr = raw_stderr + "\n--- CONTINUATION PASS STDERR ---\n" + cont_stderr
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
                            "continuation: both passes emitted complete_run — "
                            "the continuation's wins (%d total in merged list)",
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
            content=raw_stdout + ("\n--- STDERR ---\n" + raw_stderr if raw_stderr else ""),
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
        usage_snapshot = UsageSnapshot(
            wall_seconds=wall,
            tool_turns=len(envelope.get("tool_calls", [])) if envelope else 0,
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


class OpenCodeAdapter(_CLITrackingAdapter):
    name = Executor.OPENCODE
    provider_kind = "opencode"


# Assembled by the prompt kit: shared core + the cli_tracking delta (tool
# list rendered from the registry, JSON envelope output contract, native
# opensweep_* MCP preference note).
_SYSTEM_PROMPT = build_system_prompt("cli_tracking")
# The write contract for the local-LLM/OpenCode delivery loop (G1): edit + test
# + commit in the sandbox, native opensweep_* MCP tools, no envelope.
_SYSTEM_PROMPT_WRITE = build_system_prompt("cli_tracking_write")


def _instruction(req: DispatchRequest, wall_ceiling: int | None = None) -> str:
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

{stance_block(req.policy, wall_ceiling, req.effort)}

Investigate only. Record bugs, gaps, and improvements through the final
JSON tool_calls envelope; persist durable facts with write_memory.
"""


def _write_instruction(req: DispatchRequest, wall_ceiling: int | None = None) -> str:
    """Implement/fix instruction for the write-capable tracking loop (OpenCode).

    Unlike `_instruction` (investigate-only + envelope), this tells the agent to
    make the change and COMMIT it in the sandbox clone; the platform validates
    the commits and pushes. No JSON envelope — the agent uses its native tools to
    edit files and its native opensweep_* MCP tools to report."""
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

{stance_block(req.policy, wall_ceiling, req.effort, write_run=True)}

Make the change described in the intent, run the relevant tests, and COMMIT it
in this working copy. Do NOT push. Finish by calling
`opensweep_platform_complete_run` with your end-of-run report.
"""


AdapterRegistry.register(OpenCodeAdapter())
