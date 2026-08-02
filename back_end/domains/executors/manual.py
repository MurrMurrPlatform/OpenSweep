"""Manual executor adapter — humans post results via the HTTP transport.

`dispatch()` for `manual` is a no-op that immediately marks the Run as
`running`. The human then calls the platform tools directly (typically
the HTTP surface at `/api/v1/platform-tools/*`) and finishes by calling
`complete_run`.

This adapter exists primarily to land the platform tool surface end-to-end
without needing claude_code / opencode running.
"""

from __future__ import annotations

from domains.executors.base import AdapterRegistry, DispatchRequest, DispatchResult, ExecutorAdapter
from domains.runs.schemas import Executor, RunStatus


class ManualAdapter(ExecutorAdapter):
    name = Executor.MANUAL

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        return DispatchResult(
            status=RunStatus.RUNNING,
            summary="Run dispatched to manual executor; awaiting human tool calls.",
            usage={"executor": "manual"},
        )


AdapterRegistry.register(ManualAdapter())
