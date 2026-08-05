"""ensure_converged_status_required — making opensweep/converged an actual
GitHub branch-protection requirement, not merely an advisory status (no DB
needed: a fake repo/client is enough).
"""

from types import SimpleNamespace

import pytest

from domains.delivery.services import pull_request_service


def _repo(**overrides) -> SimpleNamespace:
    defaults = dict(
        uid="repo-1", github_owner="acme", github_repo="widgets", default_branch="main"
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_delegates_to_client_with_the_converged_context(monkeypatch):
    calls = {}

    class _Client:
        async def add_required_status_check(self, owner, repo, branch, *, context):
            calls.update(owner=owner, repo=repo, branch=branch, context=context)
            return True

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    ok = await pull_request_service.ensure_converged_status_required(_repo())
    assert ok is True
    assert calls == {
        "owner": "acme",
        "repo": "widgets",
        "branch": "main",
        "context": "opensweep/converged",
    }


@pytest.mark.asyncio
async def test_no_github_coordinates_is_a_quiet_noop(monkeypatch):
    called = {"n": 0}

    class _Client:
        async def add_required_status_check(self, *a, **k):
            called["n"] += 1
            return True

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    ok = await pull_request_service.ensure_converged_status_required(
        _repo(github_owner="", github_repo="")
    )
    assert ok is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_client_without_the_capability_is_a_quiet_noop(monkeypatch):
    class _OldClient:
        pass  # no add_required_status_check — an older/other provider client

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _OldClient()
    )

    ok = await pull_request_service.ensure_converged_status_required(_repo())
    assert ok is False


@pytest.mark.asyncio
async def test_client_exception_degrades_quietly_never_raises(monkeypatch):
    class _Client:
        async def add_required_status_check(self, *a, **k):
            raise RuntimeError("network exploded")

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    ok = await pull_request_service.ensure_converged_status_required(_repo())
    assert ok is False
