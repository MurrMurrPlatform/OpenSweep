"""ensure_converged_status_required — making opensweep/converged an actual
GitHub branch-protection requirement, not merely an advisory status (no DB
needed: a fake repo/client is enough).

This MUTATES the user's repository settings, so the tests below pin two
properties: it is reached only from the explicit opt-in (never from an
implement dispatch), and creating a rule is audited.
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


@pytest.fixture
def audits(monkeypatch):
    written = []

    async def _fake_audit(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(pull_request_service, "write_audit", _fake_audit)
    return written


@pytest.mark.asyncio
async def test_delegates_to_client_with_the_converged_context(monkeypatch, audits):
    calls = {}

    class _Client:
        async def add_required_status_check(self, owner, repo, branch, *, context):
            calls.update(owner=owner, repo=repo, branch=branch, context=context)
            return "created"

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    outcome = await pull_request_service.ensure_converged_status_required(
        _repo(), actor_uid="user-9"
    )
    assert outcome == "created"
    assert calls == {
        "owner": "acme",
        "repo": "widgets",
        "branch": "main",
        "context": "opensweep/converged",
    }
    # The mutation must be attributable — it changed the user's repo settings.
    assert [a["kind"] for a in audits] == ["delivery.branch_protection_created"]
    assert audits[0]["actor_uid"] == "user-9"
    assert audits[0]["subject_uid"] == "repo-1"
    assert audits[0]["payload"]["branch"] == "main"


@pytest.mark.asyncio
async def test_no_rule_created_means_no_audit(monkeypatch, audits):
    class _Client:
        async def add_required_status_check(self, *a, **k):
            return "already-required"

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    outcome = await pull_request_service.ensure_converged_status_required(_repo())
    assert outcome == "already-required"
    assert audits == []


@pytest.mark.asyncio
async def test_no_github_coordinates_is_a_quiet_noop(monkeypatch, audits):
    called = {"n": 0}

    class _Client:
        async def add_required_status_check(self, *a, **k):
            called["n"] += 1
            return "created"

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    outcome = await pull_request_service.ensure_converged_status_required(
        _repo(github_owner="", github_repo="")
    )
    assert outcome == "failed"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_client_without_the_capability_is_a_quiet_noop(monkeypatch, audits):
    class _OldClient:
        pass  # no add_required_status_check — an older/other provider client

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _OldClient()
    )

    outcome = await pull_request_service.ensure_converged_status_required(_repo())
    assert outcome == "failed"


@pytest.mark.asyncio
async def test_client_exception_degrades_quietly_never_raises(monkeypatch, audits):
    class _Client:
        async def add_required_status_check(self, *a, **k):
            raise RuntimeError("network exploded")

    monkeypatch.setattr(
        pull_request_service, "get_provider_client", lambda repo: _Client()
    )

    outcome = await pull_request_service.ensure_converged_status_required(_repo())
    assert outcome == "failed"
    assert audits == []


def test_implement_dispatch_never_touches_branch_protection():
    """Regression: `trigger_implement_run` used to call this on EVERY implement
    dispatch, so one ticket silently created a repo-wide merge gate on the
    user's default branch. Consent now comes from MergePolicy, and the flag is
    applied where it is set (api/v1/delivery.update_merge_policy)."""
    import inspect

    from domains.delivery.services import implement_run_service

    source = inspect.getsource(implement_run_service.trigger_implement_run)
    assert "ensure_converged_status_required" not in source


def test_merge_policy_defaults_to_not_enforcing():
    """Opt-in, never opt-out: a repo that never asked for it must not get a
    branch-protection rule."""
    from domains.delivery.models import MergePolicy

    assert MergePolicy.enforce_converged_status.default is False
