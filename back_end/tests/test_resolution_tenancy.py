"""F1 (CRITICAL) — cross-org Finding→PR bind must be rejected in the service.

WHY: the platform-tool MCP endpoint `opensweep_platform_bind_finding_to_pr`
(api/v1/platform_tools_delivery.py) gates only the *PR's* repository against
the caller's run scope, then hands the client-supplied `finding_uid` straight
to `ResolutionService.ensure`. If `ensure` does not independently verify the
Finding lives in the same repository as the PR, an AI executor pinned to org
A's PR can bind org B's Finding into it — laundering B's finding
title/severity/evidence into A's convergence ledger and merge gate. The human
REST twin (api/v1/delivery.py) already blocks this with a 409; the service
(the shared choke point every caller flows through) did not.

WHAT: `ensure` must raise 404 when `finding.repository_uid != pr.repository_uid`,
and still succeed for a legitimate same-repo bind (including the idempotent
"already bound" short-circuit). DB-free: models are monkeypatched with
in-memory fakes.
"""

import pytest
from fastapi import HTTPException

import domains.delivery.services.resolution_service as svc_mod
from domains.delivery.services.resolution_service import ResolutionService

pytestmark = pytest.mark.asyncio


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        _STORE.setdefault(type(self).__name__, []).append(self)
        return self


_STORE: dict[str, list] = {}


def _nodes_for(store_key: str):
    class _Nodes:
        async def get_or_none(self, **kw):
            for n in _STORE.get(store_key, []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        async def filter(self, **kw):
            return [
                n
                for n in _STORE.get(store_key, [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]

    return _Nodes()


class FakeFinding(_Node):
    nodes = _nodes_for("FakeFinding")


class FakePullRequest(_Node):
    nodes = _nodes_for("FakePullRequest")


class FakeFindingResolution(_Node):
    nodes = _nodes_for("FakeFindingResolution")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    monkeypatch.setattr(svc_mod, "Finding", FakeFinding)
    monkeypatch.setattr(svc_mod, "PullRequest", FakePullRequest)
    monkeypatch.setattr(svc_mod, "FindingResolution", FakeFindingResolution)

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(svc_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _seed_finding(uid, repo):
    n = FakeFinding(uid=uid, repository_uid=repo, status="open", evidence={}, severity="high", title="t")
    _STORE.setdefault("FakeFinding", []).append(n)
    return n


def _seed_pr(uid, repo):
    n = FakePullRequest(uid=uid, repository_uid=repo, head_sha="abc123")
    _STORE.setdefault("FakePullRequest", []).append(n)
    return n


async def test_ensure_rejects_cross_org_finding():
    # PR is in org-A's repo; finding belongs to org-B's repo. The bind must
    # 404 (existence never leaks) instead of creating a resolution.
    _seed_pr("pr-a", "repo-a")
    _seed_finding("f-b", "repo-b")
    with pytest.raises(HTTPException) as exc:
        await ResolutionService().ensure(finding_uid="f-b", pull_request_uid="pr-a")
    assert exc.value.status_code == 404
    # nothing was written
    assert _STORE.get("FakeFindingResolution", []) == []


async def test_ensure_allows_same_repo_bind():
    # The legitimate case still works: finding and PR share a repository.
    _seed_pr("pr-a", "repo-a")
    _seed_finding("f-a", "repo-a")
    r = await ResolutionService().ensure(finding_uid="f-a", pull_request_uid="pr-a")
    assert r.repository_uid == "repo-a"
    assert r.finding_uid == "f-a"
