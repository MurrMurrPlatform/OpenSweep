"""Tenancy scope for the platform-tool surface (multi-tenancy phase 2).

Two caller kinds reach /api/v1/platform-tools* (and the MCP mount):
  - Executors holding a `osrt_…` run token: TokenAuthMiddleware verified the
    token↔run binding and stashed `run_token_uid` in the scope. Their
    authority is exactly ONE run — every request must target that run's
    repository, nothing else.
  - Humans (OIDC / shared token): normal org rules — the target repository
    must be in the caller's org.

Both checks 404 on violation (existence never leaks).
"""

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO


def _run_token_uid(connection: HTTPConnection) -> str:
    return (connection.scope.get("state") or {}).get("run_token_uid", "")


# Definitively-dead run states: no re-dispatch is possible, so the run's token
# is spent. (awaiting_input / ended / limit_exceeded / paused_quota are all
# resumable under the same run_uid, so their tokens MUST stay valid.) Rejecting
# a spent token narrows the window a leaked osrt_ token stays usable — the token
# is otherwise stateless and never expires. A leaked token from a run that is
# still resumable is not fully closed here; that needs per-run token rotation.
_DEAD_RUN_STATUSES = frozenset({"failed", "cancelled"})


async def _run_repo_and_status(run_uid: str) -> tuple[str, str]:
    from domains.runs.models import Run

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return "", ""
    return (run.repository_uid or ""), (run.status or "")


async def require_tool_repo_access(
    connection: HTTPConnection, user: UserDTO, repository_uid: str | None
) -> None:
    """Gate a platform-tool call that targets a repository."""
    token_run = _run_token_uid(connection)
    if token_run:
        run_repo, run_status = await _run_repo_and_status(token_run)
        if not run_repo or (repository_uid or "") != run_repo:
            raise HTTPException(status_code=404, detail="not found")
        if run_status in _DEAD_RUN_STATUSES:
            raise HTTPException(status_code=404, detail="not found")
        return
    await require_repo_in_org(repository_uid, user.org_uid)


async def require_tool_run_access(
    connection: HTTPConnection, user: UserDTO, run_uid: str
) -> None:
    """Gate a platform-tool call that targets a run (e.g. complete-run)."""
    token_run = _run_token_uid(connection)
    if token_run:
        if run_uid != token_run:
            raise HTTPException(status_code=404, detail="not found")
        _, run_status = await _run_repo_and_status(token_run)
        if run_status in _DEAD_RUN_STATUSES:
            raise HTTPException(status_code=404, detail="not found")
        return
    repo, _ = await _run_repo_and_status(run_uid)
    await require_repo_in_org(repo, user.org_uid)
