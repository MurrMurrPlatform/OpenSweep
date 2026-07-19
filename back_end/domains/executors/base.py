"""ExecutorAdapter ABC.

PLATFORM.md §Executor return contracts: each adapter is responsible for:
1. Receiving a DispatchRequest with intent, target, context, RunPolicy.
2. Invoking its executor with the platform-tool surface accessible via
   that executor's native tool-call mechanism (or via HTTP transport for
   `manual` / non-Python executors).
3. Bridging the executor's output back via the platform tools — preferred
   path first, structured-blob fallback, raw-transcript last resort.
4. Always retaining `raw_artifact_uri` regardless of which path
   succeeded.
5. Enforcing operational ceilings from the RunPolicy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from domains.runs.schemas import (
    ExecutionMode,
    Executor,
    RunStatus,
    RunTrigger,
)
from domains.run_policies.models import RunPolicy


@dataclass
class DispatchRequest:
    """Everything an adapter needs to run."""

    run_uid: str
    scheduled_agent_uid: str
    repository_uid: str
    repository_local_path: str | None
    intent: str
    target: dict[str, Any] = field(default_factory=dict)
    # Pre-rendered docs/memory briefing (KNOWLEDGE_V3): pinned Docs verbatim,
    # unpinned index, target-anchored memories. Inserted into prompts as-is.
    context: str = ""
    mode: ExecutionMode = ExecutionMode.ANALYZE_ONLY
    trigger: RunTrigger = RunTrigger.MANUAL
    triggered_by: str = ""
    policy: RunPolicy | None = None
    # Provider chosen by the lifecycle (active or §8 fallback-chain pick).
    # Empty = adapters fall back to the active provider (legacy behaviour).
    provider_uid: str = ""
    # Per-stage workflow overrides (repositories/services/workflow.py).
    # model_override replaces the provider's model for this run only;
    # max_wall_seconds_override outranks the policy ceiling AND the
    # local-provider skip — an explicit per-stage ceiling always applies.
    model_override: str = ""
    max_wall_seconds_override: int = 0


@dataclass
class DispatchResult:
    """Adapters return this; the lifecycle service writes it to the Run."""

    status: RunStatus
    raw_artifact_uri: str = ""
    parse_status: str = "ok"
    usage: dict[str, Any] = field(default_factory=dict)
    output_refs: list[str] = field(default_factory=list)
    error: str = ""
    summary: str = ""
    # Structured end-of-run summary the agent left in its trailer
    # complete_run call ({did, skipped, succeeded, failed, next_steps}).
    # Executors whose agents call complete_run over MCP mid-run leave this
    # empty — the tool call already persisted it on the Run.
    outcome: dict[str, Any] = field(default_factory=dict)


class ExecutorAdapter(ABC):
    """Base class. Concrete adapters live alongside (claude_code.py, etc.)."""

    name: Executor

    @abstractmethod
    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        ...

    async def dry_run(self, req: DispatchRequest) -> dict[str, Any]:
        """Override in adapters that have a meaningful estimator. Default
        is generic: defer to `dry_run.estimate()`."""
        from domains.run_policies.services.dry_run import estimate

        return estimate(
            executor=self.name,
            intent=req.intent,
            scope_paths=len(req.target.get("paths", []) or []),
        ).to_dict()


class AdapterRegistry:
    """Lookup adapters by executor name. Populated at import time by each
    adapter module via `register()`."""

    _by_name: dict[Executor, ExecutorAdapter] = {}

    @classmethod
    def register(cls, adapter: ExecutorAdapter) -> None:
        cls._by_name[adapter.name] = adapter

    @classmethod
    def get(cls, name: Executor) -> ExecutorAdapter:
        if name not in cls._by_name:
            raise KeyError(f"no adapter registered for executor {name.value}")
        return cls._by_name[name]

    @classmethod
    def available(cls) -> list[Executor]:
        return list(cls._by_name.keys())
