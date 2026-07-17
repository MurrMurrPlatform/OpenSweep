"""Shared executor-adapter plumbing.

Extracted from claude_code.py / cli_tracking.py / internal_llm.py (audit #33),
which triplicated:

- provider resolution (explicit provider_uid pick → active fallback)
- the wall-ceiling resolution ladder (per-stage override > local-provider
  skip > policy > system default)
- run input recording + live stream recording (now a debounced, append-only
  `StreamRecorder` — audit #35)
- JSON envelope extraction and the trailer/envelope platform-tool dispatch
  loop (incl. complete_run harvesting and output_refs extraction)
- live ceiling enforcement feeding real usage (audit #48)

Adapters keep only argv/prompt construction and executor-specific bits.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from domains.executors.base import DispatchRequest
from domains.investigations.models import Run
from domains.investigations.services.run_events import append_event, preview
from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services.llm_executor import is_local_provider_kind
from domains.llm_providers.services.llm_provider_service import (
    get_active_provider,
    repository_org_uid,
)
from domains.platform_tools.complete_run import extract_outcome
from domains.platform_tools.dispatcher import dispatch as dispatch_platform_tool
from domains.run_policies.models import RunPolicy
from domains.run_policies.services.ceilings import CeilingExceeded, UsageSnapshot
from domains.run_policies.services.ceilings import check as check_ceilings
from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS
from infrastructure import artifact_store
from logging_config import logger

# Keys a platform-tool result may carry that become Run.output_refs entries.
OUTPUT_REF_KEYS = ("finding_uid", "memory_uid", "doc_edit_uid", "artifact_uri")


# ── Provider + ceiling resolution ─────────────────────────────────────────


async def resolve_provider(
    provider_uid: str = "", kind: str = "", repository_uid: str = ""
) -> LLMProvider | None:
    """The provider the lifecycle chose (if any), else the active provider.

    `kind` restricts acceptance to one provider kind (claude_code /
    cli_tracking adapters); empty accepts anything (internal_llm).

    Tenancy: both the explicit pick and the active fallback are restricted to
    the run's org's OWN providers (there is no shared/platform scope), so an
    executor can never run on — or read the credentials of — another org's
    provider.
    """
    org_uid = await repository_org_uid(repository_uid) if repository_uid else ""
    if provider_uid:
        chosen = await LLMProvider.nodes.get_or_none(uid=provider_uid)
        if (
            chosen is not None
            and (not kind or (chosen.kind or "").strip() == kind)
            and org_uid
            and (getattr(chosen, "org_uid", "") or "") == org_uid
        ):
            return chosen
    active = await get_active_provider(org_uid)
    if active is not None and (not kind or (active.kind or "").strip() == kind):
        return active
    return None


def resolve_wall_ceiling(req: DispatchRequest, provider_kind: str) -> int | None:
    """Effective wall ceiling for the run; None disables the guard.

    Ladder: an explicit per-stage override outranks everything (including
    the local-provider skip); local providers otherwise run unbounded (the
    user pays with their own electricity, not metered tokens); else the
    policy ceiling; else the system default.
    """
    if req.max_wall_seconds_override:
        return int(req.max_wall_seconds_override)
    if is_local_provider_kind(provider_kind):
        return None
    if req.policy and req.policy.max_wall_seconds:
        return int(req.policy.max_wall_seconds)
    return DEFAULT_MAX_WALL_SECONDS


# ── Ceiling enforcement (audit #48) ──────────────────────────────────────


class _EffectivePolicy:
    """RunPolicy view whose max_wall_seconds is the adapter's *effective*
    wall ceiling (per-stage override / local-provider skip applied), so the
    live check agrees with the timeout that was actually enforced."""

    def __init__(self, policy: RunPolicy, wall_ceiling: int | None) -> None:
        self._policy = policy
        self._wall_ceiling = wall_ceiling

    @property
    def max_wall_seconds(self):  # noqa: ANN201 — mirrors RunPolicy's loose typing
        return self._wall_ceiling

    def __getattr__(self, name: str) -> Any:
        return getattr(self._policy, name)


def enforce_ceilings(
    *,
    policy: RunPolicy | None,
    usage: UsageSnapshot,
    wall_ceiling: int | None,
) -> tuple[list[str], CeilingExceeded | None]:
    """Live ceiling check. Returns (soft_warnings, exceeded-or-None).

    Hard exceedances are returned (not raised) so adapters can assemble
    their executor-specific LIMIT_EXCEEDED DispatchResult. `wall_ceiling`
    must be the same value used for the subprocess timeout (None = the
    local-provider skip / no wall guard)."""
    if policy is None:
        return [], None
    try:
        warnings = check_ceilings(
            policy=_EffectivePolicy(policy, wall_ceiling),
            usage=usage,
            raise_on_exceed=True,
        )
    except CeilingExceeded as exc:
        return [], exc
    return warnings, None


def exceeded_usage(exc: CeilingExceeded, **usage: Any) -> dict[str, Any]:
    """The `usage` payload for a LIMIT_EXCEEDED DispatchResult."""
    return {
        **usage,
        "exceeded": {"field": exc.field, "value": exc.value, "ceiling": exc.ceiling},
    }


# ── Run input + live stream recording ─────────────────────────────────────


async def record_input(run_uid: str, *, system_prompt: str, instruction: str) -> None:
    """Persist the rendered prompts on Run.usage for the UI."""
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return
    usage = dict(run.usage or {})
    usage["rendered_system_prompt"] = system_prompt
    usage["rendered_instruction"] = instruction
    run.usage = usage
    await run.save()


def line_aligned_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    first_newline = tail.find("\n")
    return tail[first_newline + 1 :] if first_newline != -1 else tail


@dataclass
class _StreamState:
    pending: list[str] = field(default_factory=list)
    total_len: int = 0
    tail: str = ""


class StreamRecorder:
    """Debounced live-transcript recorder (audit #35).

    Replaces the per-line `_record_stream` copies, which re-fetched the Run,
    rewrote the ENTIRE cumulative artifact to disk and saved the node on
    every output line (O(n²) I/O), with the stdout/stderr pumps racing each
    other on the usage dict.

    - persistence is debounced: at most every `flush_interval_seconds` or
      `flush_bytes` of new output, plus a final flush from `close()`;
    - the live artifact file is appended to, never rewritten (the adapter's
      final `artifact_store.put` overwrites it with the canonical layout);
    - all state lives behind one asyncio.Lock, so the stdout and stderr
      pumps can't lose each other's usage updates.
    """

    def __init__(
        self,
        *,
        run_uid: str,
        repository_uid: str,
        label: str = "live transcript",
        tail_chars: int = 500_000,
        flush_interval_seconds: float = 3.0,
        flush_bytes: int = 64 * 1024,
    ) -> None:
        self._run_uid = run_uid
        self._repository_uid = repository_uid
        self._label = label
        self._tail_chars = tail_chars
        self._flush_interval = flush_interval_seconds
        self._flush_bytes = flush_bytes
        self._lock = asyncio.Lock()
        self._states: dict[str, _StreamState] = {
            "stdout": _StreamState(),
            "stderr": _StreamState(),
        }
        self._pending_bytes = 0
        self._last_flush = time.monotonic()
        self._live_uri = ""
        self._last_appended_stream = "stdout"
        self._closed = False

    @property
    def live_uri(self) -> str:
        return self._live_uri

    async def record_total(self, stream: str, text: str) -> None:
        """Record the *running total* for a stream (llm_executor's on_chunk
        contract); only the unseen suffix is buffered."""
        async with self._lock:
            state = self._states.setdefault(stream, _StreamState())
            if len(text) <= state.total_len:
                return
            self._buffer(state, stream, text[state.total_len :])
            await self._maybe_flush_locked()

    async def record_delta(self, stream: str, delta: str) -> None:
        """Record one new chunk for a stream (line pumps)."""
        if not delta:
            return
        async with self._lock:
            state = self._states.setdefault(stream, _StreamState())
            self._buffer(state, stream, delta)
            await self._maybe_flush_locked()

    async def close(self) -> None:
        """Final flush; safe to call more than once."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._flush_locked()

    def _buffer(self, state: _StreamState, stream: str, delta: str) -> None:
        state.pending.append(delta)
        state.total_len += len(delta)
        self._pending_bytes += len(delta)

    async def _maybe_flush_locked(self) -> None:
        if self._pending_bytes >= self._flush_bytes or (
            time.monotonic() - self._last_flush
        ) >= self._flush_interval:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if self._pending_bytes == 0:
            return
        try:
            self._append_to_artifact_locked()
        except OSError as exc:
            logger.warning(
                f"StreamRecorder: live artifact append failed for run {self._run_uid}: {exc}",
                extra={"tag": "executor_stream"},
            )
        self._pending_bytes = 0
        self._last_flush = time.monotonic()
        try:
            await self._save_run_locked()
        except Exception as exc:  # noqa: BLE001 — streaming must never break the run
            logger.warning(
                f"StreamRecorder: usage flush failed for run {self._run_uid}: {exc}",
                extra={"tag": "executor_stream"},
            )

    def _append_to_artifact_locked(self) -> None:
        if not self._live_uri:
            # Establish the artifact file + URI once; everything after is append.
            self._live_uri = artifact_store.put(
                repository_uid=self._repository_uid,
                run_uid=self._run_uid,
                content="",
                artifact_type="raw_transcript",
                extension="txt",
                summary=self._label,
            )
        # Private-by-convention helper: the store's URI→path mapping is the
        # append seam; adding a public append() would be an artifact_store
        # API change outside this refactor's blast radius.
        path = artifact_store._path_from_uri(self._live_uri)  # noqa: SLF001
        chunks: list[bytes] = []
        for stream in ("stdout", "stderr"):
            state = self._states[stream]
            if not state.pending:
                continue
            joined = "".join(state.pending)
            state.pending.clear()
            state.tail = line_aligned_tail(state.tail + joined, self._tail_chars)
            if stream != self._last_appended_stream:
                chunks.append(f"\n--- {stream.upper()} ---\n".encode())
            self._last_appended_stream = stream
            chunks.append(joined.encode("utf-8", errors="replace"))
        if chunks and path is not None:
            with open(path, "ab") as fh:
                fh.write(b"".join(chunks))

    async def _save_run_locked(self) -> None:
        run = await Run.nodes.get_or_none(uid=self._run_uid)
        if run is None:
            return
        usage = dict(run.usage or {})
        for stream, state in self._states.items():
            if state.total_len:
                usage[f"stream_{stream}"] = state.tail
        run.usage = usage
        if self._live_uri:
            run.raw_artifact_uri = self._live_uri
        await run.save()


# ── Envelope extraction + platform-tool dispatch loop ────────────────────


def extract_envelope(text: str) -> dict[str, Any] | None:
    """Best-effort: pull the final `{"tool_calls": […]}` JSON envelope out
    of a transcript (fenced ```json block preferred, else last { … })."""
    s = (text or "").strip()
    if not s:
        return None
    if "```" in s:
        parts = s.split("```")
        for i in range(len(parts) - 2, 0, -2):
            block = parts[i]
            if block.startswith("json"):
                block = block[4:]
            try:
                obj = json.loads(block.strip())
                if isinstance(obj, dict) and "tool_calls" in obj:
                    return obj
            except json.JSONDecodeError:
                continue
    # Fall back to the last balanced-brace object in the text. A naive
    # rfind("{")/rfind("}") breaks on nested objects (the last "{" is an
    # inner one), so scan for depth-0 spans instead — string-aware so braces
    # inside JSON string values don't throw off the depth count.
    for start, end in reversed(_balanced_brace_spans(s)):
        try:
            obj = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tool_calls" in obj:
            return obj
    return None


def _balanced_brace_spans(s: str) -> list[tuple[int, int]]:
    """Return (start, end) index pairs for every top-level {...} span in ``s``,
    tracking string literals and escapes so braces inside strings are ignored."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(s):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    spans.append((start, i))
    return spans


# Envelope tools that reference an existing object by uid instead of carrying
# a repository_uid; their tenancy gate resolves the target's repository.
_BY_UID_TOOLS = frozenset({"update_finding", "attach_artifact"})

# Tenancy violations read as "not found" — existence never leaks (tenancy.py).
_SCOPE_ERROR = "not found"


async def _envelope_target_repository_uid(name: str, args: dict[str, Any]) -> str:
    """repository_uid of the object a by-uid envelope call targets ("" when
    the target does not exist). Mirrors the per-endpoint resolution in
    `api/v1/platform_tools.py`. Lazy imports — these domains import the
    executors package back at call time."""
    if name == "update_finding":
        from domains.findings.models import Finding

        node = await Finding.nodes.get_or_none(uid=str(args.get("finding_uid") or ""))
        return (node.repository_uid or "") if node else ""
    # attach_artifact — same target map as _artifact_target_repository_uid.
    from domains.delivery.models import PullRequest
    from domains.docs.models import Doc
    from domains.findings.models import Finding
    from domains.memory.models import Memory
    from domains.tickets.models import Ticket

    models = {
        "run": Run,
        "finding": Finding,
        "doc": Doc,
        "memory": Memory,
        "ticket": Ticket,
        "pull_request": PullRequest,
        "pullrequest": PullRequest,
    }
    model = models.get(str(args.get("target_type") or "").strip().lower())
    node = await model.nodes.get_or_none(uid=str(args.get("target_uid") or "")) if model else None
    return (node.repository_uid or "") if node else ""


async def execute_envelope_tool_calls(
    *,
    calls: Any,
    req: DispatchRequest,
    executor_value: str,
    deny_tools: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Execute a trailer/envelope's platform tool calls server-side.

    Returns (tool_results, output_refs, outcome). `complete_run` entries are
    never dispatched — finalization is the lifecycle's job — only their
    structured end-of-run summary is harvested into `outcome`. `deny_tools`
    maps a tool name to the error recorded instead of dispatching it (e.g.
    patch tools in tracking-only v1).

    Tenancy: the envelope is model-emitted text, so its args are untrusted.
    Scope keys (`repository_uid`, `source_run_uid`, `executor`) are FORCED to
    the run's own values — override, never setdefault — and by-uid calls must
    target an object inside the run's repository, mirroring what
    `api/platform_scope.require_tool_repo_access` does for the HTTP surface.
    A run's authority is exactly one repository; without this a run scoped to
    repo A could read from or write into any other org's data by naming a
    foreign uid.
    """
    results: list[dict[str, Any]] = []
    refs: list[str] = []
    outcome: dict[str, Any] = {}
    if not isinstance(calls, list):
        return results, refs, outcome
    deny_tools = deny_tools or {}
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool")
        if not isinstance(name, str) or not name:
            continue
        if name == "complete_run":
            outcome = extract_outcome(dict(call.get("args") or {}))
            continue
        if name in deny_tools:
            results.append({"tool": name, "error": deny_tools[name]})
            continue
        args = dict(call.get("args") or {})
        if name in _BY_UID_TOOLS:
            target_repo = await _envelope_target_repository_uid(name, args)
            if not target_repo or target_repo != req.repository_uid:
                results.append({"tool": name, "error": _SCOPE_ERROR})
                append_event(
                    req.run_uid, "tool_result", name=name, output=_SCOPE_ERROR, is_error=True
                )
                continue
            if name == "attach_artifact":
                args["repository_uid"] = req.repository_uid
                args["executor"] = executor_value
        else:
            args["repository_uid"] = req.repository_uid
            args["source_run_uid"] = req.run_uid
            args["executor"] = executor_value
        append_event(req.run_uid, "tool_use", name=name, input=preview(args))
        try:
            result = await dispatch_platform_tool(name, **args)
            results.append({"tool": name, "result": result})
            append_event(req.run_uid, "tool_result", name=name, output=preview(result), is_error=False)
            for key in OUTPUT_REF_KEYS:
                if isinstance(result, dict) and result.get(key):
                    refs.append(f"{key}:{result[key]}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{executor_value}: trailer tool {name!r} failed: {exc}")
            results.append({"tool": name, "error": str(exc)})
            append_event(req.run_uid, "tool_result", name=name, output=str(exc), is_error=True)
    return results, refs, outcome
