"""P0 — the freshness reconcile backstop, against the real test Neo4j.

Push webhooks are the realtime path for marking Areas/Docs stale. Before this
existed, a single dropped delivery left the whole board reading "fresh" forever
with nothing able to notice or repair it. These tests pin the repair loop and,
just as importantly, the cases where it must NOT mark anything.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from domains.areas.models import Area, area_is_stale
from domains.repositories.models import Repository
from domains.repositories.services import freshness_sync
from domains.repositories.services.freshness_sync import (
    reconcile_repository_freshness,
    sync_push_freshness,
)

pytestmark = pytest.mark.integration


class _FakeClient:
    """Stands in for the git provider. `compare` maps base..head → paths."""

    def __init__(self, head="", compare=None, fail=False):
        self._head = head
        self._compare = compare or {}
        self._fail = fail
        self.is_active = True

    async def get_branch_head_sha(self, owner, repo, branch):
        if self._fail:
            raise RuntimeError("github down")
        return self._head

    async def compare_commits(self, owner, repo, base, head):
        if self._fail:
            raise RuntimeError("github down")
        return self._compare.get((base, head), {"paths": [], "truncated": False})


def _install(monkeypatch, client):
    import infrastructure.git_providers as providers

    monkeypatch.setattr(providers, "get_provider_client", lambda repo: client)


async def _make_repo(**kw) -> Repository:
    r = Repository(
        uid="repo-" + uuid4().hex[:8],
        org_uid="org-" + uuid4().hex[:6],
        slug="s" + uuid4().hex[:6],
        mode="github",
        name="repo",
        default_branch="main",
        github_owner="acme",
        github_repo="api",
        **kw,
    )
    await r.save()
    return r


async def _make_area(repo_uid: str, key: str, scopes: list[str]) -> Area:
    a = Area(
        uid=uuid4().hex,
        repository_uid=repo_uid,
        key=key,
        kind="subsystem",
        scope_paths=list(scopes),
        last_reviewed_at=datetime.now(UTC) - timedelta(days=1),
    )
    await a.save()
    return a


async def test_first_sync_adopts_head_without_marking_anything(monkeypatch):
    """A newly registered repo must not be born stale against its entire
    history — that says nothing about whether the map is accurate."""
    repo = await _make_repo()
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(monkeypatch, _FakeClient(head="h1"))

    result = await reconcile_repository_freshness(repo)

    assert not result.applied
    assert "adopted current head" in result.reason
    assert repo.freshness_synced_sha == "h1"
    assert not area_is_stale(await Area.nodes.get(uid=area.uid))


async def test_reconcile_replays_the_gap_a_dropped_webhook_left(monkeypatch):
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(
        monkeypatch,
        _FakeClient(head="h2", compare={("h1", "h2"): {"paths": ["src/api/x.py"], "truncated": False}}),
    )

    result = await reconcile_repository_freshness(repo)

    assert result.applied and result.areas_marked == 1
    assert area_is_stale(await Area.nodes.get(uid=area.uid))
    assert repo.freshness_synced_sha == "h2"


async def test_reconcile_is_a_noop_when_already_at_head(monkeypatch):
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(monkeypatch, _FakeClient(head="h1"))

    result = await reconcile_repository_freshness(repo)

    assert not result.applied and result.reason == "already at head"
    assert not area_is_stale(await Area.nodes.get(uid=area.uid))


async def test_failed_compare_leaves_the_cursor_behind_so_the_next_tick_retries(monkeypatch):
    """Advancing past a range we could not read would silently skip it
    forever — the whole failure mode this backstop exists to prevent."""
    repo = await _make_repo(freshness_synced_sha="h1")
    _install(monkeypatch, _FakeClient(head="h2", fail=True))

    result = await reconcile_repository_freshness(repo)

    assert not result.applied
    assert repo.freshness_synced_sha == "h1"  # cursor did NOT advance


async def test_paths_outside_every_scope_mark_nothing(monkeypatch):
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(
        monkeypatch,
        _FakeClient(head="h2", compare={("h1", "h2"): {"paths": ["docs/readme.md"], "truncated": False}}),
    )

    result = await reconcile_repository_freshness(repo)

    assert result.applied and result.areas_marked == 0
    assert not area_is_stale(await Area.nodes.get(uid=area.uid))
    assert repo.freshness_synced_sha == "h2"


async def test_truncated_compare_is_recorded_as_degraded(monkeypatch):
    """A partial path list must not render as a confident all-fresh board."""
    repo = await _make_repo(freshness_synced_sha="h1")
    await _make_area(repo.uid, "api", ["src/api"])
    _install(
        monkeypatch,
        _FakeClient(head="h2", compare={("h1", "h2"): {"paths": ["src/api/x.py"], "truncated": True}}),
    )

    result = await reconcile_repository_freshness(repo)

    assert result.applied
    assert "truncated" in result.degraded_reason
    assert "truncated" in repo.freshness_degraded_reason


# ---------- push path ----------


async def test_push_to_default_branch_marks_and_advances(monkeypatch):
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(
        monkeypatch,
        _FakeClient(compare={("h1", "h2"): {"paths": ["src/api/x.py"], "truncated": False}}),
    )

    result = await sync_push_freshness(
        repo,
        {"ref": "refs/heads/main", "before": "h1", "after": "h2", "commits": []},
    )

    assert result.applied and result.areas_marked == 1
    assert area_is_stale(await Area.nodes.get(uid=area.uid))
    assert repo.freshness_synced_sha == "h2"


async def test_push_to_other_branch_marks_nothing_and_leaves_the_cursor(monkeypatch):
    """The headline behavior change — and the cursor must not move either, or
    the reconcile tick would treat the feature-branch head as the last-seen
    default-branch commit."""
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(monkeypatch, _FakeClient())

    result = await sync_push_freshness(
        repo,
        {
            "ref": "refs/heads/feature-x",
            "before": "h1",
            "after": "h9",
            "commits": [{"added": ["src/api/x.py"], "modified": [], "removed": []}],
        },
    )

    assert not result.applied
    assert not area_is_stale(await Area.nodes.get(uid=area.uid))
    assert repo.freshness_synced_sha == "h1"


async def test_push_falls_back_to_payload_commits_when_compare_is_unavailable(monkeypatch):
    repo = await _make_repo(freshness_synced_sha="h1")
    area = await _make_area(repo.uid, "api", ["src/api"])
    _install(monkeypatch, _FakeClient(fail=True))

    result = await sync_push_freshness(
        repo,
        {
            "ref": "refs/heads/main",
            "before": "h1",
            "after": "h2",
            "commits": [{"added": ["src/api/x.py"], "modified": [], "removed": []}],
        },
    )

    assert result.applied and result.areas_marked == 1
    assert area_is_stale(await Area.nodes.get(uid=area.uid))
    assert "compare unavailable" in result.degraded_reason


async def test_reconcile_all_skips_inactive_repositories(monkeypatch):
    active = await _make_repo(freshness_synced_sha="h1", is_active=True)
    await _make_repo(freshness_synced_sha="h1", is_active=False)
    await _make_area(active.uid, "api", ["src/api"])
    _install(
        monkeypatch,
        _FakeClient(head="h2", compare={("h1", "h2"): {"paths": ["src/api/x.py"], "truncated": False}}),
    )

    out = await freshness_sync.reconcile_all_repositories()

    assert out["areas_marked"] >= 1
    assert out["repositories"] >= 1
