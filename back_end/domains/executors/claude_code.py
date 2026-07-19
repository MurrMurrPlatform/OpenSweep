"""Claude Code executor adapter.

PLATFORM.md §Phase 5: delegate to the `claude` CLI in headless mode with
`--mcp-config` pointing at the OpenSweep platform-tool MCP bridge. Tool calls
land back through HTTP into our platform tool implementations.

Return-contract fallback ladder:
  1. preferred — tool calls (Claude calls platform tools via MCP); the
     Findings/Knowledge already exist by the time the CLI exits
  2. fallback — structured-blob in the final message; we attempt a best-
     effort JSON parse
  3. last resort — raw transcript only; no placeholder Finding is filed —
     the transcript stays reachable through the run's `raw_artifact_uri`

Raw transcript is *always* persisted via `artifact_store.put()`.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from typing import Any

from config import settings
from domains.executors._shared import (
    StreamRecorder,
    _completed_via_mcp,
    budget_briefing,
    ceiling_warnings,
    execute_envelope_tool_calls,
    record_input,
    resolve_provider,
    resolve_wall_ceiling,
)
from domains.executors.base import (
    AdapterRegistry,
    DispatchRequest,
    DispatchResult,
    ExecutorAdapter,
)
from domains.executors.mcp_bridge import (
    claude_env,
    platform_mcp_url,
    probe_platform_mcp,
    write_claude_mcp_config,
)
from domains.executors.quota import detect_quota_exhaustion
from domains.executors.stream_events import ClaudeStreamTranslator, stream_event_delta
from domains.investigations.schemas import (
    ExecutionMode,
    Executor,
    RunStatus,
)
from domains.investigations.services.run_events import append_event, publish_delta
from domains.investigations.services.turn_cli import extract_claude_meta
from domains.llm_providers.schemas import effective_cli_template
from domains.llm_providers.services.credentials import provider_secret
from domains.llm_providers.services.llm_executor import with_model_flag
from domains.run_policies.services.ceilings import UsageSnapshot
from infrastructure import artifact_store
from infrastructure.code_graph import CODE_GRAPH_PROMPT as _CODE_GRAPH_BRIEFING
from infrastructure.process_tree import kill_tree, process_group_kwargs
from infrastructure.run_tokens import run_token_config_error
from logging_config import logger


class ClaudeCodeAdapter(ExecutorAdapter):
    name = Executor.CLAUDE_CODE

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        scratch_root = getattr(settings, "OPENSWEEP_SANDBOX_HOST_MOUNT", "/tmp") or "/tmp"

        provider = await resolve_provider(
            req.provider_uid, kind="claude_subscription", repository_uid=req.repository_uid
        )
        if provider is None:
            return DispatchResult(
                status=RunStatus.FAILED,
                error="active LLMProvider is not kind=claude_subscription",
                summary="claude_code requires the active provider to be claude_subscription",
            )
        if req.model_override:
            # In-memory only — per-stage workflow override, never saved.
            provider.model = req.model_override

        # Fail fast when the agent's MCP callbacks are doomed: a run whose
        # agent can't load opensweep-platform tools can't file findings, so a
        # clear dispatch error beats a silently tool-less transcript. Two
        # checks: the auth config must allow minting a run token, and the
        # platform mount must be reachable.
        config_error = run_token_config_error()
        if config_error:
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"executor MCP auth misconfigured: {config_error}",
                summary="claude_code aborted before dispatch: opensweep-platform tool calls would 401",
            )
        probe_error = await probe_platform_mcp()
        if probe_error:
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"platform MCP mount unreachable at {platform_mcp_url()}: {probe_error}",
                summary=(
                    "claude_code aborted before dispatch: the agent would run without "
                    "opensweep-platform tools (check OPENSWEEP_BACKEND_INTERNAL_URL)"
                ),
            )

        cwd = req.repository_local_path
        # The workspace was code-graph indexed at sandbox creation
        # (sandbox_service._create); here we only expose the server.
        mcp_config_path = write_claude_mcp_config(
            run_uid=req.run_uid, scratch_root=scratch_root, workspace_path=cwd or ""
        )
        if not mcp_config_path:
            logger.warning("claude_code: MCP bridge config could not be written", extra={"tag": "claude_code"})

        # The template is platform-owned — rows created before the service
        # defaulted it (or cleared by hand) fall back to the catalog default;
        # rows still holding a known legacy seeded default roll forward.
        template = effective_cli_template(provider.kind, provider.cli_command_template)
        if not template:
            return DispatchResult(
                status=RunStatus.FAILED,
                error="LLMProvider.cli_command_template is empty",
                summary="claude_code adapter requires cli_command_template on the LLMProvider",
            )

        # Mode-gated prompts (§6): tracking-only runs get the read-only
        # contract; implement/fix runs get the write contract (edit + commit
        # in the sandbox, never push — the platform validates and pushes).
        system_prompt = (
            _SYSTEM_PROMPT_WRITE if req.mode == ExecutionMode.IMPLEMENT else _SYSTEM_PROMPT
        )

        # Resolve wall ceiling FIRST so _build_instruction can include the
        # budget briefing (which needs the actual ceiling value).
        wall_ceiling = resolve_wall_ceiling(req, provider.kind)

        instruction = self._build_instruction(req, wall_ceiling)
        rendered = (
            template
            .replace("{{system_prompt}}", system_prompt)
            .replace("{{system_prompt_q}}", shlex.quote(system_prompt))
            .replace("{{instruction}}", instruction)
            .replace("{{instruction_q}}", shlex.quote(instruction))
            .replace("{{model}}", provider.model or "")
            .replace("{{model_q}}", shlex.quote(provider.model or ""))
            .replace("{{working_dir}}", cwd or "")
            .replace("{{working_dir_q}}", shlex.quote(cwd or ""))
            .replace("{{mcp_config_path}}", mcp_config_path)
            .replace("{{mcp_config_path_q}}", shlex.quote(mcp_config_path))
        )

        # Un-capped base argv: the continuation loop below applies
        # `with_turn_cap` per pass (the initial pass with the full cap, later
        # passes with the *remaining* turns), so wrapping here would double the
        # flag.
        argv = with_model_flag(
            ensure_stream_json_flags(shlex.split(rendered)),
            kind="claude_subscription",
            model=provider.model or "",
            template=template,
        )
        env = claude_env(run_uid=req.run_uid, oauth_token=provider_secret(provider))
        await record_input(
            req.run_uid,
            system_prompt=system_prompt,
            instruction=instruction,
        )
        append_event(req.run_uid, "user_message", text=instruction)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        timed_out = False
        translator = ClaudeStreamTranslator()
        recorder = StreamRecorder(
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            label="live claude_code transcript",
        )
        # Real usage from claude's `result` stream event (num_turns, token
        # counts, total_cost_usd) — feeds post-run ceiling warnings (never a
        # hard stop). `num_turns` is per-pass; we accumulate it into
        # `turns_used` across passes.
        # Token counts and cost are likewise per-pass in cli_usage (each pass
        # overwrites it); we accumulate totals in tokens_used / dollars_used.
        cli_usage: dict[str, Any] = {}
        turns_used = 0
        tokens_used = 0
        dollars_used = 0.0
        session_id = ""
        quota_hit = False
        max_extra_passes = int(getattr(settings, "OPENSWEEP_CONTINUATION_PASSES", 3))
        turn_cap = (
            int(req.policy.max_tool_turns)
            if (req.policy and req.policy.max_tool_turns)
            else None
        )
        investigate_wall = soft_wall(wall_ceiling)

        async def _run_pass(pass_argv: list[str], timeout: float | None) -> tuple[int | None, bool, str, str]:
            """One CLI invocation. Returns (exit_code, timed_out, pass_stdout,
            pass_stderr) — both slices are per-pass so quota detection sees only
            this pass's stderr, not the cumulative stream across passes."""
            nonlocal cli_usage
            pass_offset = len(stdout_parts)
            pass_stderr_offset = len(stderr_parts)
            pass_timed_out = False
            proc = await asyncio.create_subprocess_exec(
                *pass_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or None,
                limit=16 * 1024 * 1024,
                # Group leader, so the wall-ceiling kill reaches the CLI's
                # MCP/Bash children too (see process_tree).
                **process_group_kwargs(),
            )

            async def _pump(stream, parts):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    if parts is stdout_parts:
                        # Partial-message stream_events are ephemeral: fan the
                        # token deltas out to live watchers and keep the lines
                        # out of the raw artifact and the events file — the
                        # complete message line that follows is the durable
                        # record.
                        delta = stream_event_delta(text)
                        if delta is not None:
                            if delta:
                                publish_delta(req.run_uid, delta)
                            continue
                        parts.append(text)
                        # Structured transcript (V3 §4): the server parses the
                        # stream; the UI renders events. Best-effort.
                        for event in translator.translate(text):
                            if event.get("type") == "turn_end" and isinstance(event.get("usage"), dict):
                                cli_usage.update(event["usage"])
                            append_event(req.run_uid, event.pop("type"), **event)
                    else:
                        parts.append(text)
                    await recorder.record_delta(
                        "stdout" if parts is stdout_parts else "stderr", text
                    )

            try:
                pumps = asyncio.gather(
                    _pump(proc.stdout, stdout_parts),
                    _pump(proc.stderr, stderr_parts),
                    proc.wait(),
                )
                if timeout is None:
                    await pumps
                else:
                    await asyncio.wait_for(pumps, timeout=timeout)
            except TimeoutError:
                pass_timed_out = True
                kill_tree(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            return (
                proc.returncode,
                pass_timed_out,
                "".join(stdout_parts[pass_offset:]),
                "".join(stderr_parts[pass_stderr_offset:]),
            )

        def _remaining(ceiling: int | None) -> float | None:
            if ceiling is None:
                return None
            return ceiling - (time.monotonic() - started)

        try:
            pass_no = 0
            while True:
                remaining = _remaining(investigate_wall)
                if pass_no == 0:
                    pass_argv = with_turn_cap(argv, turn_cap)
                else:
                    if remaining is not None and remaining < _MIN_CONTINUATION_SECONDS:
                        break
                    remaining_turns = (
                        max(1, turn_cap - turns_used) if turn_cap else None
                    )
                    cont = build_continuation_argv(
                        argv,
                        instruction=instruction,
                        nudge=_CONTINUATION_NUDGE,
                        session_id=session_id,
                    )
                    if cont is None:
                        break
                    pass_argv = with_turn_cap(cont, remaining_turns)
                    append_event(req.run_uid, "user_message", text=_CONTINUATION_NUDGE)

                exit_code, timed_out, pass_stdout, pass_stderr = await _run_pass(pass_argv, remaining)
                turns_used += int(cli_usage.get("num_turns") or 0)
                tokens_used += int(cli_usage.get("input_tokens") or 0) + int(cli_usage.get("output_tokens") or 0)
                dollars_used += float(cli_usage.get("total_cost_usd") or 0.0)
                for line in pass_stdout.splitlines():
                    meta = extract_claude_meta(line)
                    if meta.session_id:
                        session_id = meta.session_id
                if timed_out:
                    break
                if detect_quota_exhaustion(exit_code, pass_stdout, pass_stderr):
                    quota_hit = True
                    break
                if await _completed_via_mcp(req.run_uid):
                    break
                if turn_cap and turns_used >= turn_cap:
                    break
                if exit_code not in (0, None):
                    break  # real CLI failure; turn-cap stops are caught by the turns_used check above
                pass_no += 1
                if pass_no > max_extra_passes:
                    break

            # Whether the run hit its budget BEFORE the wind-down pass. The
            # wind-down reuses `exit_code`/`timed_out` (its own clean exit would
            # otherwise erase a soft-wall kill from the investigate loop), so
            # capture the budget-stop signal here and OR it into the post-loop
            # decision below.
            budget_stopped_before_winddown = bool(
                timed_out or (turn_cap and turns_used >= turn_cap)
            )

            # Wind-down: budget ran out (wall soft-kill, turn cap, or pass cap)
            # before complete_run — spend the reserved wall share on a wrap-up
            # pass so the run ends with a report instead of a cliff.
            if (
                session_id
                and not quota_hit
                and not await _completed_via_mcp(req.run_uid)
            ):
                winddown_budget = _remaining(wall_ceiling)
                if winddown_budget is None or winddown_budget > 30:
                    wind_argv = build_continuation_argv(
                        argv,
                        instruction=instruction,
                        nudge=_WINDDOWN_NUDGE,
                        session_id=session_id,
                    )
                    if wind_argv is not None:
                        append_event(req.run_uid, "user_message", text=_WINDDOWN_NUDGE)
                        exit_code, timed_out, _, _ = await _run_pass(
                            with_turn_cap(wind_argv, 30), winddown_budget
                        )
                        turns_used += int(cli_usage.get("num_turns") or 0)
                        tokens_used += int(cli_usage.get("input_tokens") or 0) + int(cli_usage.get("output_tokens") or 0)
                        dollars_used += float(cli_usage.get("total_cost_usd") or 0.0)
        except FileNotFoundError as exc:
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"claude CLI not found: {exc}",
                summary="claude_code adapter requires the `claude` CLI on PATH",
            )
        finally:
            await recorder.close()
            if session_id:
                await _persist_session_id(req.run_uid, session_id)

        wall_seconds = time.monotonic() - started
        raw_stdout = "".join(stdout_parts)
        raw_stderr = "".join(stderr_parts)

        raw_uri = artifact_store.put(
            repository_uid=req.repository_uid,
            run_uid=req.run_uid,
            content=raw_stdout + ("\n--- STDERR ---\n" + raw_stderr if raw_stderr else ""),
            artifact_type="raw_transcript",
            extension="txt",
            summary="claude_code raw transcript",
        )

        # Tool calls usually write Findings/Knowledge into the platform during
        # the run via MCP. We also parse a structured trailer if the agent
        # emitted one and execute those platform tool calls server-side.
        parse_status, parsed_meta = self._parse_trailer(raw_stdout)

        # Quota is a state, not a failure (§8): pause instead of failing when
        # the CLI died on a usage/rate limit. An agent SUCCESS (exit 0 with a
        # completed tool flow, i.e. a structured trailer) is never quota.
        completed_tool_flow = (
            exit_code == 0 and not timed_out and bool(parsed_meta.get("trailer"))
        )
        if (
            not timed_out
            and not completed_tool_flow
            and detect_quota_exhaustion(exit_code, raw_stdout, raw_stderr)
        ):
            return DispatchResult(
                status=RunStatus.PAUSED_QUOTA,
                raw_artifact_uri=raw_uri,
                parse_status=parse_status,
                usage={
                    "wall_seconds": round(wall_seconds, 2),
                    "exit_code": exit_code,
                },
                error="provider quota/rate limit reached",
                summary="paused: provider quota exhausted — will retry",
            )

        trailer = parsed_meta.get("trailer")
        tool_results, parsed_refs, trailer_outcome = await execute_envelope_tool_calls(
            calls=trailer.get("tool_calls") if isinstance(trailer, dict) else None,
            req=req,
            executor_value=Executor.CLAUDE_CODE.value,
        )

        # Post-run ceiling accounting (Task 5): warnings only — a finished run
        # is never retroactively failed; LIMIT_EXCEEDED is reserved for runs a
        # limit actually stopped (wall kill / turn cap). tool_turns, tokens, and
        # dollars are all accumulated across every pass (including wind-down);
        # cli_usage stays as-is (last pass's raw values for the usage payload).
        usage_snapshot = UsageSnapshot(
            wall_seconds=wall_seconds,
            tool_turns=turns_used,
            tokens=tokens_used,
            dollars=dollars_used,
        )
        warnings = ceiling_warnings(
            policy=req.policy, usage=usage_snapshot, wall_ceiling=wall_ceiling
        )

        # Did the agent deliberately finish (complete_run)? Checked once more
        # after the wind-down pass, which may itself have called complete_run.
        completed = await _completed_via_mcp(req.run_uid)
        # OR in the pre-wind-down signal: a soft-wall-killed run whose wind-down
        # exits cleanly still stopped on budget (the wind-down overwrote
        # `timed_out`). A wind-down that itself times out also counts.
        budget_stopped = (
            budget_stopped_before_winddown
            or timed_out
            or (turn_cap and turns_used >= turn_cap)
        ) and not completed

        if budget_stopped:
            # The continuation + wind-down loop ran out of wall/turn budget
            # before the agent called complete_run. LIMIT_EXCEEDED is in
            # FOLLOW_UP_STATUSES, so the UI composer stays enabled and — with
            # cli_session_id persisted above — a follow-up message resumes the
            # same CLI session.
            status = RunStatus.LIMIT_EXCEEDED
            err = "run budget exhausted (wall/turns) — resumable from the UI"
        elif exit_code not in (0, None):
            status = RunStatus.FAILED
            err = f"claude CLI exited {exit_code}"
        else:
            status = RunStatus.AWAITING_INPUT
            err = ""

        return DispatchResult(
            status=status,
            raw_artifact_uri=raw_uri,
            parse_status=parse_status,
            usage={
                "wall_seconds": round(wall_seconds, 2),
                "exit_code": exit_code,
                "parsed_meta": parsed_meta,
                "tool_calls": len(tool_results),
                "tool_results": tool_results,
                "warnings": warnings,
                "cli_usage": cli_usage,
                "continuation_passes": pass_no,
                "turns_used": turns_used,
                "tokens_used": tokens_used,
                "dollars_used": dollars_used,
            },
            output_refs=[raw_uri, *parsed_refs],
            error=err,
            summary=f"claude_code finished in {wall_seconds:.1f}s (exit={exit_code})",
            # Fallback only: when the agent called complete_run over MCP the
            # structured summary is already on the Run and this won't clobber it.
            outcome=trailer_outcome,
        )

    def _build_instruction(self, req: DispatchRequest, wall_ceiling: int | None = None) -> str:
        target_blob = json.dumps(req.target or {}, indent=2)
        ctx_blob = req.context or "(no additional context provided)"
        template = (
            _USER_TEMPLATE_WRITE if req.mode == ExecutionMode.IMPLEMENT else _USER_TEMPLATE
        )
        return template.format(
            intent=req.intent,
            mode=req.mode.value,
            target=target_blob,
            context=ctx_blob,
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            budget=budget_briefing(req.policy, wall_ceiling),
        )

    def _parse_trailer(self, raw_stdout: str) -> tuple[str, dict[str, Any]]:
        """Best-effort: look for a final JSON object/array in the transcript."""
        s = (raw_stdout or "").strip()
        if not s:
            return "failed", {}
        # Strip a fenced ```json block if present.
        if "```" in s:
            parts = s.split("```")
            for i in range(len(parts) - 2, 0, -2):
                block = parts[i]
                if block.startswith("json"):
                    block = block[4:]
                try:
                    return "ok", {"trailer": json.loads(block.strip())}
                except json.JSONDecodeError:
                    continue
        # Look for trailing { ... } or [ ... ].
        for opener, closer in (("{", "}"), ("[", "]")):
            start = s.rfind(opener)
            end = s.rfind(closer)
            if start != -1 and end != -1 and end > start:
                try:
                    return "ok", {"trailer": json.loads(s[start : end + 1])}
                except json.JSONDecodeError:
                    continue
        # No structured trailer — but if the run used MCP tool calls, that's the
        # preferred path and parse_status is still ok. We can't easily tell from
        # the transcript alone whether tool calls fired; we record `degraded`
        # only if the transcript is essentially empty.
        return ("ok" if len(s) > 32 else "degraded", {})


def ensure_stream_json_flags(argv: list[str]) -> list[str]:
    """Guarantee `--output-format stream-json --verbose
    --include-partial-messages` on the claude argv.

    Streamed JSONL is what feeds the live log; partial messages add the
    token-level stream_event deltas the live watchers render. An operator-set
    --output-format is respected (they chose a format deliberately); the
    companion flags are only added when stream-json is in effect (the CLI
    requires --verbose with stream-json in `-p` mode).
    """
    argv = list(argv)
    if "--output-format" not in argv:
        argv += ["--output-format", "stream-json"]
    try:
        fmt = argv[argv.index("--output-format") + 1]
    except IndexError:
        fmt = ""
    if fmt == "stream-json":
        if "--verbose" not in argv:
            argv.append("--verbose")
        if "--include-partial-messages" not in argv:
            argv.append("--include-partial-messages")
    return argv


def with_turn_cap(argv: list[str], max_turns: int | None) -> list[str]:
    """Delegate the policy's turn ceiling to the CLI (`--max-turns` stops the
    loop cleanly between turns). An operator-set flag in the template wins."""
    if not max_turns or max_turns <= 0 or "--max-turns" in argv:
        return list(argv)
    return [*argv, "--max-turns", str(int(max_turns))]


# Continuation loop (see docs/superpowers/plans/2026-07-19-run-depth-and-policy-overhaul.md):
# a headless `-p` turn ends whenever the model emits a final message, which on
# huge open-ended tasks is reliably too early. While the agent has not called
# complete_run (it stamps Run.completed_at via MCP) and budget remains, resume
# the CLI session and tell it to continue. The last _WINDDOWN_SHARE of the wall
# is reserved for one wrap-up pass so runs end with a report, not a kill.
_WINDDOWN_SHARE = 0.10
_MIN_CONTINUATION_SECONDS = 120

_CONTINUATION_NUDGE = """Continue this run — it is not finished. You stopped without calling the
`complete_run` platform tool, so OpenSweep resumed your session. Pick up
exactly where you left off: work through every remaining area of your plan,
file each new issue with `create_finding` as you find it, and only when the
whole scope is genuinely covered finish with `complete_run`. Do not repeat
work you already recorded and do not re-file findings you already filed."""

_WINDDOWN_NUDGE = """Your run budget is exhausted — do NOT investigate anything new. Wrap up now:
file any findings you have evidence for but have not yet filed, record the
areas you did not reach as skipped, and call `complete_run` with your
end-of-run report. This is your final pass."""


def soft_wall(wall_ceiling: int | None) -> int | None:
    """The investigation portion of the wall: kill at this point, then spend
    the reserved remainder on one wind-down pass."""
    if wall_ceiling is None:
        return None
    return max(1, int(wall_ceiling * (1 - _WINDDOWN_SHARE)))


def build_continuation_argv(
    argv: list[str], *, instruction: str, nudge: str, session_id: str
) -> list[str] | None:
    """Continuation-pass argv: same invocation with the -p payload swapped for
    the nudge, resuming the recorded session. None (no continuation possible)
    when there is no session id or a custom template inlined the instruction
    in a way we cannot find."""
    if not session_id:
        return None
    out: list[str] = []
    replaced = False
    for token in argv:
        if not replaced and token == instruction:
            out.append(nudge)
            replaced = True
        else:
            out.append(token)
    if not replaced:
        return None
    return [*out, "--resume", session_id]


async def _persist_session_id(run_uid: str, session_id: str) -> None:
    """The UI's follow-up turns (turn_service) resume Run.cli_session_id —
    recording it here keeps executor runs continuable from the UI."""
    from domains.investigations.models import Run

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is not None and session_id:
        run.cli_session_id = session_id
        await run.save()


# Shared between the read and write prompts: MCP servers can still be
# mid-handshake when the turn starts, and there is no human in a headless
# run to answer a "should I retry?" question.
_MCP_STARTUP_NOTE = """Your MCP servers may still be connecting when you start. If the
`opensweep-platform` tools are not yet in your tool list (or a tool search finds
none), continue the task with your native tools and retry loading them later
in the run — they usually appear within seconds. This is a headless run with
NO human present: never ask whether to retry or wait for confirmation, and
never finish without calling the required opensweep-platform tools."""


_SYSTEM_PROMPT = """You are a Claude Code agent running inside OpenSweep — a tracking-only repo
intelligence platform. You have access to OpenSweep's platform-tool MCP server
(`opensweep-platform`) with the following write tools:

  create_finding, update_finding, propose_doc_edit, confirm_doc_current,
  write_memory, attach_artifact, complete_run

and read tools for platform state: list_docs, read_doc, search_memory.

""" + _CODE_GRAPH_BRIEFING + """

""" + _MCP_STARTUP_NOTE + """

You MUST record durable output through OpenSweep tools. For every bug, docs gap,
missing capability, stale assumption, or improvement you discover, call the
MCP tool `mcp__opensweep-platform__opensweep_platform_create_finding` immediately
(shown in some prompts as `opensweep_platform_create_finding` or
`create_finding`). Do not wait until the end and do not leave observations
only in prose.

Subagents may inspect code and docs, but the top-level agent is responsible
for filing OpenSweep findings. A subagent summary is not a durable result.

If you find no actionable issue, still call the create-finding tool once with
kind=`observation`, severity=`low`, subtype=`no-actionable-finding`, and
evidence describing what you checked. Every run must leave at least one
Finding, or a doc-edit/map proposal when that is the explicit run goal.

Do not edit files, produce patches, run code-changing commands, commit, open
PRs, or ask OpenSweep to apply changes. You are here to inspect, document, and
record findings only.

Treat incomplete or stale documentation inside the repository as a Finding
tagged `docs`. Use `write_memory` for small durable facts future runs should
know: gotchas, decisions, non-obvious constraints — one paragraph, never
anything derivable from the code. Use `propose_doc_edit` to improve OpenSweep's
documentation pages (conventions, architecture, features) when they are
wrong, missing, or bloated; read the current page with `read_doc` first.

When you finish, ALWAYS call `complete_run` with an end-of-run report:
`summary` (one short paragraph), plus the structured lists `did` (what you
did), `skipped` (what you skipped and why), `succeeded` (what succeeded),
`failed` (what failed and why), and `next_steps` (follow-ups or future
suggestions). One short sentence per entry; omit lists you have nothing
for. This report is stored on the Run and shown to humans — write it for
someone who did not watch the run."""


_SYSTEM_PROMPT_WRITE = """You are a Claude Code agent running inside OpenSweep on a WRITE run
(implement or fix). You are working in a disposable sandbox clone with the
correct work branch already checked out.

Your job: make the minimal code change described in the intent, run the
relevant tests, and COMMIT the result inside this working copy.

Hard rules — the platform enforces these after the run and will discard
non-compliant work:
- NEVER push. NEVER run `git push`, `git pull`, or `git fetch`. The platform
  validates your commits and pushes with its own credentials.
- NEVER switch branches, force anything, or rewrite history (no rebase,
  no --amend on commits you did not create in this run, no reset --hard).
- NEVER touch paths matching the forbidden patterns listed in the intent.
- Commit with clear conventional commit messages as instructed in the intent.

You still have access to OpenSweep's platform-tool MCP server (`opensweep-platform`).
Use the tools the intent names (e.g. `opensweep_platform_attach_fix` on fix runs)
and ALWAYS finish with `complete_run`, giving an end-of-run report: `summary`
(one short paragraph covering the commits you made — shas + messages — and
the test results), plus the structured lists `did`, `skipped` (and why),
`succeeded`, `failed` (and why), and `next_steps` (follow-ups or future
suggestions). This report is stored on the Run and shown to humans — write
it for someone who did not watch the run.

""" + _CODE_GRAPH_BRIEFING + """

""" + _MCP_STARTUP_NOTE


_USER_TEMPLATE = """# Run

repository_uid: {repository_uid}
run_uid:        {run_uid}
mode:           {mode}

# Intent

{intent}

# Target

```json
{target}
```

# Context

{context}

{budget}

# Instructions

Use your native tools (Read/Glob/Grep/Bash; avoid Edit/Write) to investigate.
Work the intent to completion — do not stop early because the task is large.
Whenever you find something worth recording, call a `opensweep-platform` tool to
push it back into OpenSweep immediately. Use `create_finding` for
bugs/gaps/improvements, `propose_*` tools for structural/doc proposals,
and `attach_artifact` for logs, traces, or notes.

Before finishing, verify that the top-level agent has called at least one
OpenSweep write tool. If there are no actionable findings, create a low-severity
observation finding that explains what was checked. Finish with
`complete_run`, reporting what you did, skipped, what succeeded, what
failed, and next steps.
"""


_USER_TEMPLATE_WRITE = """# Run

repository_uid: {repository_uid}
run_uid:        {run_uid}
mode:           {mode}

# Intent

{intent}

# Target

```json
{target}
```

# Context

{context}

{budget}

# Instructions

This is a WRITE run: use your native tools (Read/Glob/Grep/Bash/Edit/Write)
to implement the intent minimally in the current working directory, run the
tests it names (or the repo's discoverable test suites), and commit the
result. Do NOT push — the platform validates your commits and pushes.
Respect the forbidden path patterns listed in the intent. Finish with
`complete_run`, listing your commits (shas + messages) and test results in
the summary, plus what you did, skipped, what succeeded, what failed, and
next steps.
"""


AdapterRegistry.register(ClaudeCodeAdapter())
