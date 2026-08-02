"""A run token (osrt_) is spent once its run is definitively dead.

The token is stateless and never expires, so a leak grants perpetual write to
the run's repo through the tool surface. Rejecting calls from a failed/cancelled
run narrows that window. Resumable states (awaiting_input / ended / paused_quota
/ limit_exceeded) re-dispatch under the same run_uid, so their tokens MUST stay
valid — the test pins that distinction. DB-free.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.platform_scope as ps
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


def _conn(run_token_uid=""):
    return SimpleNamespace(scope={"state": {"run_token_uid": run_token_uid}})


def _user():
    return UserDTO(uid="u", email="e@x.y", display_name="U", role="viewer", org_uid="org-a")


def _patch_run(monkeypatch, repo, status):
    async def _fake(run_uid):
        return (repo, status)

    monkeypatch.setattr(ps, "_run_repo_and_status", _fake)


@pytest.mark.parametrize("dead", ["failed", "cancelled"])
async def test_dead_run_token_rejected(monkeypatch, dead):
    _patch_run(monkeypatch, "repo-1", dead)
    with pytest.raises(HTTPException) as exc:
        await ps.require_tool_repo_access(_conn("run-1"), _user(), "repo-1")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("alive", ["running", "awaiting_input", "ended", "paused_quota", "limit_exceeded"])
async def test_live_or_resumable_run_token_allowed(monkeypatch, alive):
    _patch_run(monkeypatch, "repo-1", alive)
    await ps.require_tool_repo_access(_conn("run-1"), _user(), "repo-1")  # no raise


async def test_run_access_dead_rejected(monkeypatch):
    _patch_run(monkeypatch, "repo-1", "failed")
    with pytest.raises(HTTPException) as exc:
        await ps.require_tool_run_access(_conn("run-1"), _user(), "run-1")
    assert exc.value.status_code == 404
