"""internal_llm executor adapter.

PLATFORM.md §Executors / §Internal LLM read tools: a lightweight in-
process LLM call with the small read toolkit + the platform tool
surface. API runs get exact token/dollar metering where the provider
surfaces it; local providers (MLX/LMStudio/Ollama) are usage-proxy only.

This v1 adapter runs a single-shot prompt against the configured
LLMProvider, then parses the response for a JSON trailer of tool calls
and dispatches them sequentially. A multi-turn loop is a future
enhancement.

Shared plumbing (provider/ceiling resolution, stream recording, envelope
extraction + tool dispatch, warnings-only ceiling accounting) lives in `_shared.py`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from domains.executors._shared import (
    StreamRecorder,
    ceiling_warnings,
    execute_envelope_tool_calls,
    extract_envelope,
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
from domains.executors.prompt_kit import stance_block, system_prompt
from domains.executors.reasoning import reasoning_args
from domains.runs.schemas import Executor, RunStatus
from domains.runs.services.run_events import append_event
from domains.llm_providers.services.llm_executor import (
    invoke as invoke_provider,
)
from domains.platform_tools.complete_run import extract_outcome
from domains.platform_tools.read_tools import call_read_tool, read_tool_names
from domains.run_policies.services.ceilings import UsageSnapshot
from infrastructure import artifact_store

_PRICING_PER_1K_TOK = {
    "claude_api": {"input": 0.003, "output": 0.015},
    "openai_api": {"input": 0.0025, "output": 0.01},
}
_LOCAL_KINDS = {"mlx", "lmstudio", "ollama", "opencode"}


class InternalLLMAdapter(ExecutorAdapter):
    name = Executor.INTERNAL_LLM

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        provider = await resolve_provider(req.provider_uid, repository_uid=req.repository_uid)
        if provider is None:
            return DispatchResult(
                status=RunStatus.FAILED,
                error="no usable active LLMProvider configured",
                summary="internal_llm needs an active provider for this organization in Settings → LLM Providers",
            )
        if req.model_override:
            # In-memory only — per-stage workflow override, never saved.
            provider.model = req.model_override

        # Pre-fetch any context the agent might want as read-tool output to
        # save round-trips. Heuristic: always run a `prior_findings` scan.
        prior = []
        try:
            prior = await call_read_tool(
                "prior_findings", repository_uid=req.repository_uid, limit=10
            )
        except Exception:
            prior = []

        # Resolve the wall ceiling FIRST so the instruction can include the
        # budget+stance block (which needs the actual ceiling value).
        timeout = resolve_wall_ceiling(req, provider.kind)

        sys_prompt = _SYSTEM_PROMPT
        instruction = _instruction(req, prior=prior, wall_ceiling=timeout)
        await record_input(
            req.run_uid,
            system_prompt=sys_prompt,
            instruction=instruction,
        )
        append_event(req.run_uid, "user_message", text=instruction)

        # on_chunk delivers the running TOTAL per stream; the transcript wants
        # only the new tail, as assistant_text chunks (merged in the UI).
        streamed_len = {"stdout": 0}
        recorder = StreamRecorder(
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            label="live internal_llm transcript",
        )

        async def _on_chunk(stream: str, text: str) -> None:
            if stream == "stdout":
                delta = text[streamed_len["stdout"]:]
                if delta:
                    streamed_len["stdout"] = len(text)
                    append_event(req.run_uid, "assistant_text", text=delta)
            await recorder.record_total(stream, text)

        # Reasoning level → HTTP request overrides (anthropic thinking block,
        # OpenAI-compat reasoning_effort, local suppress_thinking).
        try:
            inv = await invoke_provider(
                provider,
                system_prompt=sys_prompt,
                instruction=instruction,
                timeout_seconds=timeout,
                on_chunk=_on_chunk,
                run_uid=req.run_uid,
                api_overrides=reasoning_args(req.reasoning, provider.kind).get("api") or {},
            )
        finally:
            await recorder.close()
        wall = time.monotonic() - started

        raw_uri = artifact_store.put(
            repository_uid=req.repository_uid,
            run_uid=req.run_uid,
            content=(inv.raw_output or "") + (
                "\n--- STDERR ---\n" + inv.stderr if inv.stderr else ""
            ),
            artifact_type="raw_transcript",
            extension="txt",
            summary="internal_llm raw transcript",
        )

        # Parse the trailer for a JSON envelope of tool calls.
        envelope = extract_envelope(inv.raw_output or "")
        parse_status = "ok" if envelope else "degraded"
        tool_results: list[dict[str, Any]] = []
        output_refs: list[str] = [raw_uri]
        outcome: dict[str, Any] = {}
        if envelope:
            tool_results, refs, outcome = await execute_envelope_tool_calls(
                calls=envelope.get("tool_calls"),
                req=req,
                executor_value=Executor.INTERNAL_LLM.value,
            )
            output_refs.extend(refs)
            if not outcome:
                # No complete_run in the trailer — the envelope's one-line
                # summary is still better than the synthetic adapter text.
                outcome = extract_outcome({"summary": envelope.get("summary")})

        # Cost: API kinds with known pricing → estimate from char counts.
        kind = (provider.kind or "").strip()
        tokens_in = len(instruction) // 4
        tokens_out = len(inv.raw_output or "") // 4
        dollars: float = 0.0
        if kind in _PRICING_PER_1K_TOK:
            p = _PRICING_PER_1K_TOK[kind]
            dollars = round((tokens_in / 1000.0) * p["input"] + (tokens_out / 1000.0) * p["output"], 6)
        elif kind in _LOCAL_KINDS:
            dollars = 0.0

        # Post-run ceiling accounting (Task 5): warnings only — a finished run
        # is never retroactively failed; LIMIT_EXCEEDED is reserved for runs a
        # limit actually stopped (wall kill surfaces as inv.error "timed out").
        # `timeout` already encodes the local-provider wall skip.
        usage_snapshot = UsageSnapshot(
            wall_seconds=wall,
            tool_turns=len(envelope.get("tool_calls", [])) if envelope else 0,
            tokens=tokens_in + tokens_out,
            dollars=dollars,
        )
        warnings = ceiling_warnings(
            policy=req.policy, usage=usage_snapshot, wall_ceiling=timeout
        )

        wall_killed = inv.error.startswith("timed out") if inv.error else False
        if wall_killed:
            status = RunStatus.LIMIT_EXCEEDED
        elif inv.error:
            status = RunStatus.FAILED
        else:
            status = RunStatus.AWAITING_INPUT

        return DispatchResult(
            status=status,
            raw_artifact_uri=raw_uri,
            parse_status=parse_status,
            usage={
                "wall_seconds": round(wall, 2),
                "tokens": tokens_in + tokens_out,
                "dollars": dollars if kind in _PRICING_PER_1K_TOK else 0.0,
                "provider_kind": kind,
                "tool_calls": len(envelope.get("tool_calls", [])) if envelope else 0,
                "tool_results": tool_results,
                "warnings": warnings,
            },
            output_refs=output_refs,
            error=inv.error or "",
            summary=(
                f"internal_llm via {kind} in {wall:.1f}s "
                f"({len(tool_results)} tool calls)"
            ),
            outcome=outcome,
        )


# Assembled by the prompt kit: shared core + the internal_llm delta (three
# tool groups rendered from the registry, JSON envelope output contract).
_SYSTEM_PROMPT = system_prompt("internal_llm")


def _instruction(
    req: DispatchRequest,
    *,
    prior: list[dict[str, Any]],
    wall_ceiling: int | None = None,
) -> str:
    return _USER_TEMPLATE.format(
        intent=req.intent,
        mode=req.mode.value,
        target=json.dumps(req.target or {}, indent=2),
        context=req.context or "(no documentation yet)",
        run_uid=req.run_uid,
        repository_uid=req.repository_uid,
        prior=json.dumps(prior, indent=2),
        read_tools=", ".join(read_tool_names()),
        budget=stance_block(req.policy, wall_ceiling, req.effort),
    )


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

{context}

# Prior open Findings (you may build on these)

```json
{prior}
```

# Read tools available

{read_tools}

{budget}

# Instructions

Investigate and emit a single JSON envelope at the end of your response
listing the platform tool calls you want the server to execute. The envelope
must include at least one durable output call: a `create_finding`,
`propose_doc_edit`, `write_memory`, or a `confirm_doc_current` /
`confirm_area_current` review stamp. Only if the envelope would otherwise be
empty, create a low-severity observation finding that states what was checked.
"""


AdapterRegistry.register(InternalLLMAdapter())
