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
    ceiling_warnings,
    execute_envelope_tool_calls,
    extract_envelope,
    record_input,
    resolve_provider,
    resolve_wall_ceiling,
)
from domains.executors.prompt_kit import stance_block, system_prompt
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
from domains.executors.reasoning import reasoning_args
from domains.executors.stream_events import ClaudeStreamTranslator, stream_event_delta
from domains.runs.schemas import (
    ExecutionMode,
    Executor,
    RunStatus,
)
from domains.runs.services.run_events import (
    append_event,
    append_event_async,
    publish_delta,
)
from domains.runs.services.turn_cli import extract_claude_meta
from domains.llm_providers.schemas import effective_cli_template
from domains.llm_providers.services.credentials import provider_secret
from domains.llm_providers.services.llm_executor import with_model_flag
from domains.run_policies.services.ceilings import UsageSnapshot
from infrastructure import artifact_store
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
        wall_ceiling = resolve_wall_ceiling(req, provider)

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
        # Reasoning level → the CLI's thinking budget (MAX_THINKING_TOKENS).
        env.update(reasoning_args(req.reasoning, provider.kind).get("env") or {})
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
        # Token counts and cost are per-pass in cli_usage (reset at the top of
        # each _run_pass, then populated from that pass's turn_end event); we
        # accumulate the running totals in tokens_used / dollars_used.
        cli_usage: dict[str, Any] = {}
        turns_used = 0
        tokens_used = 0
        dollars_used = 0.0
        session_id = ""
        quota_hit = False
        # Extra continuation passes: the policy's ceiling when one is present
        # (None = unbounded — the loop is then wall-limited only); the settings
        # fallback applies only to policy-less dispatches.
        max_extra_passes: int | None
        if req.policy is not None:
            raw_passes = req.policy.max_continuation_passes
            max_extra_passes = int(raw_passes) if raw_passes is not None else None
        else:
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
            # Reset per-pass usage BEFORE the pass runs: a pass killed (soft-wall
            # timeout, crash, quota) emits no turn_end usage event, so without
            # this the stale previous-pass values would be added a second time
            # by the accumulate step — inflating turns/tokens/dollars and able to
            # trip a ceiling at a fraction of the real spend.
            cli_usage.clear()
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
                            # Hot path: keep the per-line disk write off the loop.
                            await append_event_async(req.run_uid, event.pop("type"), **event)
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
                completed = await _completed_via_mcp(req.run_uid)
                # Quota is decided from stdout/stderr TEXT, so an agent SUCCESS
                # — exit 0 with a completed tool flow (here: it called
                # complete_run over MCP) — is never quota, even if its own
                # output discusses quotas/rate-limits (e.g. a review of
                # rate-limiting code). Mirrors the post-loop completed_tool_flow
                # exemption; without it the run ends early with no verdict.
                if not (exit_code == 0 and completed) and detect_quota_exhaustion(
                    exit_code, pass_stdout, pass_stderr
                ):
                    quota_hit = True
                    break
                if completed:
                    break
                if turn_cap and turns_used >= turn_cap:
                    break
                if exit_code not in (0, None):
                    break  # real CLI failure; turn-cap stops are caught by the turns_used check above
                pass_no += 1
                if max_extra_passes is not None and pass_no > max_extra_passes:
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
        # extract_envelope handles fenced blocks, trailing objects, and bare
        # tool-call arrays (wrapped as {"tool_calls": [...]}).
        envelope = extract_envelope(raw_stdout)
        parsed_meta: dict[str, Any] = {"trailer": envelope} if envelope else {}
        stripped = (raw_stdout or "").strip()
        if not stripped:
            parse_status = "failed"
        elif envelope is not None or len(stripped) > 32:
            # No structured trailer — but if the run used MCP tool calls,
            # that's the preferred path and parse_status is still ok. We
            # record `degraded` only if the transcript is essentially empty.
            parse_status = "ok"
        else:
            parse_status = "degraded"

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
            budget=stance_block(
                req.policy,
                wall_ceiling,
                req.effort,
                write_run=req.mode == ExecutionMode.IMPLEMENT,
            ),
        )


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
    from domains.runs.models import Run

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is not None and session_id:
        run.cli_session_id = session_id
        await run.save()


# Mode-gated system prompts, assembled by the prompt kit (shared core +
# claude-specific deltas: MCP naming/startup notes, write-mode hard rules).
_SYSTEM_PROMPT = system_prompt("claude_code_read")
_SYSTEM_PROMPT_WRITE = system_prompt("claude_code_write")


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
OpenSweep write tool — a finding, a `propose_*` edit, or a
`confirm_doc_current`/`confirm_area_current` review stamp all count. Only if
none of those happened, create a low-severity observation finding that
explains what was checked. Finish with
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
