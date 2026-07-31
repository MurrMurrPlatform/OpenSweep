"""Explicit repo selection (§7) — offline unit tests.

Covers: cross-matching available repos to registered Repository nodes
(mark_registered, pure), the register-repo membership check
(find_installation_repo, pure), pagination + cap of
list_installation_repositories (mocked httpx), the webhook behavior change
(installation events LINK but never CREATE repos), and both endpoints'
shapes/validation via TestClient with the DB/GitHub seams monkeypatched.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.v1.github_app as app_module
import api.v1.github_webhooks as webhooks_module
from api.v1.github_app import find_installation_repo, mark_registered
from app import app as real_app
from config import settings
from infrastructure import github_app, github_app_store, redis_client
from tests.test_github_app import configure_github_app


@pytest.fixture(autouse=True)
def isolated_app_store(monkeypatch, tmp_path):
    """Every test starts with no App configured and empty caches."""
    from tests.fake_redis import FakeAsyncRedis

    monkeypatch.setattr(settings, "ARTIFACT_STORE_ROOT", str(tmp_path / "var" / "artifacts"))
    monkeypatch.setattr(settings, "GITHUB_APP_ID", "")
    monkeypatch.setattr(settings, "GITHUB_APP_SLUG", "")
    monkeypatch.setattr(settings, "GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setattr(settings, "GITHUB_PRIVATE_KEY_PATH", "")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    fake_redis = FakeAsyncRedis()
    monkeypatch.setattr(redis_client, "get_async_redis", lambda: fake_redis)

    async def _no_pat_connections(org_uid):
        return []

    # Offline tests: the endpoints' PAT-connection reads never hit the graph.
    monkeypatch.setattr(app_module, "org_pat_connections", _no_pat_connections)
    github_app_store._invalidate_cache()
    asyncio.run(github_app.clear_token_cache())
    yield
    github_app_store._invalidate_cache()
    asyncio.run(github_app.clear_token_cache())


def _gh_repo(repo_id, owner, name, **extra):
    return {
        "id": repo_id,
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner},
        "default_branch": extra.get("default_branch", "main"),
        "private": extra.get("private", False),
        "description": extra.get("description", ""),
    }


class _Node(SimpleNamespace):
    """Repository stand-in: attribute bag + async save()."""

    async def save(self):
        self.saved = getattr(self, "saved", 0) + 1


def _repo_node(**kw):
    defaults = dict(
        uid="uid-x",
        slug="repo",
        mode="github",
        name="repo",
        description="",
        default_branch="main",
        color_scheme="indigo",
        is_active=True,
        github_owner=None,
        github_repo=None,
        github_repo_id=None,
        github_installation_id=None,
        github_connection_status=None,
        last_synced_at=None,
        metadata={},
        kill_switch_active=False,
        created_at=None,
        updated_at=None,
    )
    defaults.update(kw)
    return _Node(**defaults)


class _Nodes:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    async def all(self):
        return list(self._nodes)

    async def filter(self, **kw):
        return [n for n in self._nodes if all(getattr(n, k, None) == v for k, v in kw.items())]

    async def get_or_none(self, **kw):
        matches = await self.filter(**kw)
        return matches[0] if matches else None


def _fake_repository(node_list):
    """A Repository substitute exposing only `.nodes` — instantiating it
    raises, so any code path that still auto-creates nodes fails loudly."""
    fake_nodes = _Nodes(node_list)

    class FakeRepository:
        nodes = fake_nodes

        def __init__(self, *a, **kw):
            raise AssertionError("Repository must not be created by this code path")

    return FakeRepository


@pytest.fixture(autouse=True)
def fake_installation_links(monkeypatch):
    """GitConnection (installation→org tenancy links) without a DB.
    Starts empty; link_installation() saves land in the same store."""
    store = _Nodes([])

    class FakeGitConnection:
        nodes = store

        def __init__(self, **kw):
            defaults = dict(provider="github", external_id="", display_name="")
            defaults.update(kw)
            self.__dict__.update(defaults)

        async def save(self):
            store._nodes.append(self)
            return self

    monkeypatch.setattr(app_module, "GitConnection", FakeGitConnection)
    monkeypatch.setattr(webhooks_module, "GitConnection", FakeGitConnection)
    return store


# ── mark_registered (pure) ───────────────────────────────────────────────────


def test_mark_registered_matches_by_repo_id_first():
    existing = [
        _repo_node(uid="u1", github_repo_id=11, github_owner="other", github_repo="misnamed")
    ]
    [m] = mark_registered([_gh_repo(11, "acme", "api")], existing)
    assert m["registered"] is True
    assert m["repository_uid"] == "u1"


def test_mark_registered_falls_back_to_owner_name_case_insensitive():
    existing = [
        _repo_node(uid="u2", github_repo_id=None, github_owner="ACME", github_repo="API")
    ]
    [m] = mark_registered([_gh_repo(12, "acme", "api")], existing)
    assert m["registered"] is True and m["repository_uid"] == "u2"


def test_mark_registered_unmatched_and_shape():
    [m] = mark_registered(
        [_gh_repo(13, "acme", "web", private=True, description="frontend", default_branch="dev")],
        [_repo_node(uid="u3", github_repo_id=99, github_owner="acme", github_repo="other")],
    )
    assert m == {
        "owner": "acme",
        "name": "web",
        "full_name": "acme/web",
        "repo_id": 13,
        "default_branch": "dev",
        "private": True,
        "description": "frontend",
        "registered": False,
        "repository_uid": "",
    }


def test_mark_registered_tolerates_sparse_github_payloads():
    [m] = mark_registered([{"name": "bare"}], [])
    assert m["owner"] == "" and m["full_name"] == "bare" and m["repo_id"] == 0
    assert m["registered"] is False


# ── find_installation_repo (pure membership check) ───────────────────────────


def test_find_installation_repo_membership():
    repos = [_gh_repo(1, "acme", "api"), _gh_repo(2, "acme", "web")]
    assert find_installation_repo(repos, owner="acme", name="web")["id"] == 2
    assert find_installation_repo(repos, owner="ACME", name="Api")["id"] == 1  # case-insensitive
    assert find_installation_repo(repos, owner="acme", name="nope") is None
    assert find_installation_repo(repos, owner="evil", name="api") is None
    assert find_installation_repo(repos, owner="", name="api") is None
    assert find_installation_repo([], owner="acme", name="api") is None


# ── list_installation_repositories — pagination + cap (mocked httpx) ─────────


def _pages_client(pages):
    """pages: list of (repositories, next_url). Returns (FakeAsyncClient, calls)."""
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, repos, next_url):
            self._repos, self._next = repos, next_url

        def raise_for_status(self):
            pass

        def json(self):
            return {"total_count": 999, "repositories": self._repos}

        @property
        def links(self):
            return {"next": {"url": self._next}} if self._next else {}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            calls.append(url)
            repos, next_url = pages[len(calls) - 1]
            return FakeResponse(repos, next_url)

    return FakeAsyncClient, calls


@pytest.fixture
def _installation_token(monkeypatch):
    async def fake_token(installation_id):
        return "ghs_test"

    monkeypatch.setattr(github_app, "get_installation_token", fake_token)


async def test_list_installation_repositories_follows_link_header(monkeypatch, _installation_token):
    page1 = [_gh_repo(i, "acme", f"r{i}") for i in range(100)]
    page2 = [_gh_repo(i, "acme", f"r{i}") for i in range(100, 150)]
    client_cls, calls = _pages_client(
        [(page1, "https://api.github.com/installation/repositories?per_page=100&page=2"),
         (page2, None)]
    )
    monkeypatch.setattr(github_app.httpx, "AsyncClient", client_cls)

    repos = await github_app.list_installation_repositories(55)
    assert len(repos) == 150
    assert calls == [
        "/installation/repositories?per_page=100",
        "https://api.github.com/installation/repositories?per_page=100&page=2",
    ]


async def test_list_installation_repositories_caps_at_300(monkeypatch, _installation_token):
    pages = [
        ([_gh_repo(p * 100 + i, "acme", f"r{p}-{i}") for i in range(100)], f"https://api.github.com/next?page={p + 2}")
        for p in range(5)
    ]
    client_cls, calls = _pages_client(pages)
    monkeypatch.setattr(github_app.httpx, "AsyncClient", client_cls)

    repos = await github_app.list_installation_repositories(55)
    assert len(repos) == github_app.MAX_INSTALLATION_REPOS == 300
    assert len(calls) == 3  # stops paginating once the cap is reached


# ── Webhook behavior: installations LINK, never CREATE ───────────────────────


@pytest.fixture
def _captured_audits(monkeypatch):
    audits: list[dict] = []

    async def fake_audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(webhooks_module, "write_audit", fake_audit)
    return audits


async def test_installation_created_links_registered_and_counts_available(
    monkeypatch, _captured_audits
):
    known = _repo_node(uid="u1", slug="api", github_repo_id=1, github_owner="acme",
                       github_repo="api", github_installation_id=None)
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([known]))

    result = await webhooks_module._handle_installation_event(
        event="installation",
        action="created",
        payload={
            "installation": {"id": 55},
            "repositories": [
                {"id": 1, "full_name": "acme/api", "name": "api"},
                {"id": 2, "full_name": "acme/new", "name": "new"},
            ],
        },
    )
    # Already-registered repo gets linked; the unknown one is only *available*.
    assert result["linked"] == ["api"]
    assert result["available"] == ["acme/new"]
    assert result["unlinked"] == []
    assert "registered" not in result
    assert known.github_installation_id == 55
    assert known.github_connection_status == "connected"

    kinds = [a["kind"] for a in _captured_audits]
    assert kinds == ["repository.installation_linked", "installation.repos_available"]
    available_audit = _captured_audits[-1]
    assert available_audit["payload"]["count"] == 1
    assert available_audit["payload"]["repos"] == ["acme/new"]
    assert available_audit["payload"]["installation_id"] == 55


async def test_installation_created_with_only_unregistered_repos_creates_nothing(
    monkeypatch, _captured_audits
):
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([]))
    result = await webhooks_module._handle_installation_event(
        event="installation",
        action="created",
        payload={
            "installation": {"id": 55},
            "repositories": [{"id": 9, "full_name": "acme/solo", "name": "solo"}],
        },
    )
    assert result["available"] == ["acme/solo"]
    assert result["linked"] == [] and result["unlinked"] == []
    assert [a["kind"] for a in _captured_audits] == ["installation.repos_available"]


async def test_installation_deleted_unlinks_all(monkeypatch, _captured_audits):
    mine = _repo_node(uid="u1", slug="api", github_installation_id=55,
                      github_connection_status="connected")
    other = _repo_node(uid="u2", slug="web", github_installation_id=66,
                       github_connection_status="connected")
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([mine, other]))

    result = await webhooks_module._handle_installation_event(
        event="installation", action="deleted", payload={"installation": {"id": 55}}
    )
    assert result["unlinked"] == ["api"]
    assert mine.github_installation_id is None
    assert mine.github_connection_status == "disconnected"
    assert other.github_installation_id == 66  # untouched


async def test_installation_repositories_removed_keeps_unlink_behavior(
    monkeypatch, _captured_audits
):
    gone = _repo_node(uid="u1", slug="old", github_repo_id=3, github_owner="acme",
                      github_repo="old", github_installation_id=55)
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([gone]))

    result = await webhooks_module._handle_installation_event(
        event="installation_repositories",
        action="removed",
        payload={
            "installation": {"id": 55},
            "repositories_added": [{"id": 8, "full_name": "acme/fresh", "name": "fresh"}],
            "repositories_removed": [{"id": 3, "full_name": "acme/old", "name": "old"}],
        },
    )
    assert result["unlinked"] == ["old"]
    assert result["available"] == ["acme/fresh"]
    assert gone.github_installation_id is None
    kinds = [a["kind"] for a in _captured_audits]
    assert "repository.installation_unlinked" in kinds
    assert "installation.repos_available" in kinds
    assert "repository.auto_registered" not in kinds


# ── Webhook fan-out: one GitHub repo, N tenants ──────────────────────────────


def _two_tenant_nodes():
    """The same GitHub repo (id=1, acme/api) registered by two orgs."""
    node_a = _repo_node(uid="ra", slug="api", org_uid="tenant-a", github_repo_id=1,
                        github_owner="acme", github_repo="api",
                        github_installation_id=55, github_connection_status="connected")
    node_b = _repo_node(uid="rb", slug="api", org_uid="tenant-b", github_repo_id=1,
                        github_owner="acme", github_repo="api",
                        github_installation_id=None, github_connection_status="connected")
    return node_a, node_b


async def test_pull_request_event_fans_out_to_all_tenant_repos(monkeypatch):
    """A PR delivery for a repo connected by two orgs syncs BOTH Repository
    nodes, each against its own repository_uid — no cross-tenant writes."""
    node_a, node_b = _two_tenant_nodes()
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([node_a, node_b]))

    synced: list[tuple[str, int]] = []

    class FakeService:
        async def sync_from_github(self, repository_uid, number):
            synced.append((repository_uid, number))

    class FakePullRequest:
        nodes = _Nodes([])

    monkeypatch.setattr(webhooks_module, "PullRequestService", FakeService)
    monkeypatch.setattr(webhooks_module, "PullRequest", FakePullRequest)

    result = await webhooks_module._process_delivery(
        event="pull_request",
        action="closed",
        payload={
            "repository": {"id": 1, "name": "api", "owner": {"login": "acme"}},
            "pull_request": {"number": 7},
        },
    )
    assert result["ok"] is True
    assert result["synced"] == [7, 7]
    assert synced == [("ra", 7), ("rb", 7)]  # each org's node, its own uid


async def test_push_event_fans_out_per_tenant_open_prs(monkeypatch):
    """Push deliveries derive PR numbers PER Repository node — each tenant's
    open PRs on the pushed branch sync under that tenant's uid only."""
    node_a, node_b = _two_tenant_nodes()
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([node_a, node_b]))

    synced: list[tuple[str, int]] = []

    class FakeService:
        async def sync_from_github(self, repository_uid, number):
            synced.append((repository_uid, number))

    class FakePullRequest:
        # Only tenant-a tracks an open PR on the pushed branch.
        nodes = _Nodes([SimpleNamespace(repository_uid="ra", head_ref="feat",
                                        state="open", github_number=3, pr_key="ra:3")])

    monkeypatch.setattr(webhooks_module, "PullRequestService", FakeService)
    monkeypatch.setattr(webhooks_module, "PullRequest", FakePullRequest)

    refreshed: list[str] = []

    async def fake_refresh(*, repository_uid, changed_paths, source):
        refreshed.append(repository_uid)

    from domains.agents.services import event_triggers

    monkeypatch.setattr(event_triggers, "refresh_docs_for_change", fake_refresh)

    result = await webhooks_module._process_delivery(
        event="push",
        action="",
        payload={
            "repository": {"id": 1, "name": "api", "owner": {"login": "acme"}},
            "ref": "refs/heads/feat",
            "commits": [{"added": ["a.py"], "modified": [], "removed": []}],
        },
    )
    assert result["synced"] == [3]
    assert synced == [("ra", 3)]  # tenant-b has no open PR on that branch
    # ...but freshness does NOT fire: "feat" is not either tenant's default
    # branch, and work on a feature branch is not yet true of the codebase.
    assert refreshed == []


async def test_default_branch_push_refreshes_freshness_for_every_tenant(monkeypatch):
    """Freshness fans out per tenant on a DEFAULT-branch push — the merge is
    what makes an area's map stale, and both tenants' nodes must learn it."""
    node_a, node_b = _two_tenant_nodes()
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([node_a, node_b]))

    class FakeService:
        async def sync_from_github(self, repository_uid, number):
            pass

    class FakePullRequest:
        nodes = _Nodes([])

    monkeypatch.setattr(webhooks_module, "PullRequestService", FakeService)
    monkeypatch.setattr(webhooks_module, "PullRequest", FakePullRequest)

    refreshed: list[tuple[str, tuple[str, ...]]] = []

    async def fake_refresh(*, repository_uid, changed_paths, source):
        refreshed.append((repository_uid, tuple(changed_paths)))
        from domains.agents.services.event_triggers import FreshnessRefresh

        return FreshnessRefresh()

    from domains.agents.services import event_triggers

    monkeypatch.setattr(event_triggers, "refresh_docs_for_change", fake_refresh)

    await webhooks_module._process_delivery(
        event="push",
        action="",
        payload={
            "repository": {"id": 1, "name": "api", "owner": {"login": "acme"}},
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "after": "b" * 40,
            "commits": [{"added": ["a.py"], "modified": ["b.py"], "removed": []}],
        },
    )
    # No provider client in this harness, so compare degrades and the payload's
    # own commit array is the fallback — both tenants still get the paths.
    assert [uid for uid, _ in refreshed] == ["ra", "rb"]
    assert all(set(paths) == {"a.py", "b.py"} for _, paths in refreshed)
    # The freshness cursor advanced to the pushed head on both nodes.
    assert node_a.freshness_synced_sha == "b" * 40
    assert node_b.freshness_synced_sha == "b" * 40


async def test_installation_connect_links_only_installation_orgs_node(
    monkeypatch, fake_installation_links, _captured_audits
):
    """Installation LINK events touch only the installation-org's node — the
    other tenant's node for the same repo (PAT-connected) is untouched."""
    node_a, node_b = _two_tenant_nodes()
    node_a.github_installation_id = None  # not yet linked
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([node_a, node_b]))
    fake_installation_links._nodes.append(
        SimpleNamespace(provider="github", external_id="55", org_uid="tenant-a")
    )

    result = await webhooks_module._handle_installation_event(
        event="installation_repositories",
        action="added",
        payload={
            "installation": {"id": 55},
            "repositories_added": [{"id": 1, "full_name": "acme/api", "name": "api"}],
        },
    )
    assert result["linked"] == ["api"]
    assert node_a.github_installation_id == 55
    assert node_b.github_installation_id is None  # other tenant untouched
    assert node_b.github_connection_status == "connected"


async def test_installation_removed_does_not_unlink_other_tenants_node(
    monkeypatch, _captured_audits
):
    """UNLINK only clears nodes actually carrying this installation id — a
    PAT-connected node of another org for the same repo stays connected."""
    node_a, node_b = _two_tenant_nodes()  # a: installation 55, b: PAT
    monkeypatch.setattr(webhooks_module, "Repository", _fake_repository([node_a, node_b]))

    result = await webhooks_module._handle_installation_event(
        event="installation_repositories",
        action="removed",
        payload={
            "installation": {"id": 55},
            "repositories_removed": [{"id": 1, "full_name": "acme/api", "name": "api"}],
        },
    )
    assert result["unlinked"] == ["api"]
    assert node_a.github_installation_id is None
    assert node_a.github_connection_status == "disconnected"
    assert node_b.github_installation_id is None  # untouched, still PAT-connected
    assert node_b.github_connection_status == "connected"


# ── GET /api/v1/github/app/available-repos ───────────────────────────────────


def test_available_repos_not_connected():
    res = TestClient(real_app).get("/api/v1/github/app/available-repos")
    assert res.status_code == 200
    assert res.json() == {"connected": False, "install_url": "", "installations": []}


def test_available_repos_shape_and_per_installation_errors(monkeypatch):
    configure_github_app(monkeypatch)

    async def fake_installations():
        return [
            {"id": 55, "account": {"login": "jeroen"}},
            {"id": 66, "account": {"login": "acme-org"}},
        ]

    async def fake_repos(installation_id):
        if installation_id == 66:
            raise RuntimeError("installation suspended")
        return [_gh_repo(1, "jeroen", "api"), _gh_repo(2, "jeroen", "new", private=True)]

    monkeypatch.setattr(github_app, "list_installations", fake_installations)
    monkeypatch.setattr(github_app, "list_installation_repositories", fake_repos)
    monkeypatch.setattr(
        app_module,
        "Repository",
        _fake_repository(
            [
                _repo_node(
                    uid="u1",
                    github_repo_id=1,
                    github_owner="jeroen",
                    github_repo="api",
                    org_uid="local-org",  # markers are org-scoped now
                )
            ]
        ),
    )

    res = TestClient(real_app).get("/api/v1/github/app/available-repos")
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    # install_url now carries the signed org-bound state (installation→org link)
    assert body["install_url"].startswith(
        "https://github.com/apps/opensweep-ab12cd34/installations/new?state=kis_"
    )
    assert [i["id"] for i in body["installations"]] == [55, 66]

    ok = body["installations"][0]
    assert ok["account"] == "jeroen" and ok["error"] == ""
    assert ok["repos"] == [
        {
            "owner": "jeroen", "name": "api", "full_name": "jeroen/api", "repo_id": 1,
            "default_branch": "main", "private": False, "description": "",
            "registered": True, "repository_uid": "u1",
        },
        {
            "owner": "jeroen", "name": "new", "full_name": "jeroen/new", "repo_id": 2,
            "default_branch": "main", "private": True, "description": "",
            "registered": False, "repository_uid": "",
        },
    ]

    broken = body["installations"][1]
    assert broken["account"] == "acme-org"
    assert "installation suspended" in broken["error"]
    assert broken["repos"] == []


def test_available_repos_tolerates_installations_fetch_failure(monkeypatch):
    configure_github_app(monkeypatch)

    async def boom():
        raise RuntimeError("github down")

    monkeypatch.setattr(github_app, "list_installations", boom)
    monkeypatch.setattr(app_module, "Repository", _fake_repository([]))

    res = TestClient(real_app).get("/api/v1/github/app/available-repos")
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True and body["installations"] == []


# ── POST /api/v1/github/app/register-repo ────────────────────────────────────


def test_register_repo_requires_connected_app():
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "acme", "name": "api"},
    )
    assert res.status_code == 409
    assert "no GitHub App connected" in res.json()["detail"]


def test_register_repo_rejects_repo_outside_installation(monkeypatch):
    configure_github_app(monkeypatch)

    async def fake_repos(installation_id):
        return [_gh_repo(1, "acme", "api")]

    monkeypatch.setattr(github_app, "list_installation_repositories", fake_repos)
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "acme", "name": "sneaky"},
    )
    assert res.status_code == 404
    assert "not available through this connection" in res.json()["detail"]


def test_register_repo_502_when_installation_listing_fails(monkeypatch):
    configure_github_app(monkeypatch)

    async def boom(installation_id):
        raise RuntimeError("github down")

    monkeypatch.setattr(github_app, "list_installation_repositories", boom)
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "acme", "name": "api"},
    )
    assert res.status_code == 502


def test_register_repo_409_when_already_registered_in_same_org(monkeypatch):
    configure_github_app(monkeypatch)

    async def fake_repos(installation_id):
        return [_gh_repo(1, "acme", "api")]

    monkeypatch.setattr(github_app, "list_installation_repositories", fake_repos)
    monkeypatch.setattr(
        app_module,
        "Repository",
        _fake_repository(
            [
                _repo_node(
                    uid="u-dup",
                    github_repo_id=1,
                    github_owner="acme",
                    github_repo="api",
                    org_uid="local-org",  # same org as the caller → uid may leak back
                )
            ]
        ),
    )
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "acme", "name": "api"},
    )
    assert res.status_code == 409
    assert "repository_uid=u-dup" in res.json()["detail"]


def test_register_repo_succeeds_when_other_org_registered_same_repo(monkeypatch):
    """Cross-tenant duplicates are legal: another org's registration of the
    same GitHub repo no longer blocks — the caller's org gets its own
    Repository node, and the other tenant's uid never leaks back."""
    from domains.repositories.services import registration

    configure_github_app(monkeypatch)

    async def fake_repos(installation_id):
        return [_gh_repo(1, "acme", "api")]

    monkeypatch.setattr(github_app, "list_installation_repositories", fake_repos)
    monkeypatch.setattr(
        app_module,
        "Repository",
        _fake_repository(
            [
                _repo_node(
                    uid="theirs",
                    github_repo_id=1,
                    github_owner="acme",
                    github_repo="api",
                    org_uid="tenant-a",  # a DIFFERENT org already registered it
                )
            ]
        ),
    )

    created: list[_Node] = []

    class CreatableRepository:
        nodes = _Nodes([])

        def __new__(cls, **kw):
            node = _repo_node(**kw)
            created.append(node)
            return node

    async def fake_audit(**kw):
        pass

    monkeypatch.setattr(registration, "Repository", CreatableRepository)
    monkeypatch.setattr(registration, "write_audit", fake_audit)

    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "acme", "name": "api"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["github_repo_id"] == 1
    assert "theirs" not in res.text  # the other tenant's repository_uid never leaks
    assert len(created) == 1 and created[0].org_uid == "local-org"


def test_register_repo_creates_from_live_github_data(monkeypatch):
    """Identifiers come from the client; repo_id/default_branch/description
    come from the installation's live repo list."""
    from domains.repositories.services import registration

    configure_github_app(monkeypatch)

    async def fake_repos(installation_id):
        assert installation_id == 55
        return [_gh_repo(777, "acme", "My.Repo", default_branch="develop", description="live desc")]

    monkeypatch.setattr(github_app, "list_installation_repositories", fake_repos)
    monkeypatch.setattr(app_module, "Repository", _fake_repository([]))

    created: list[_Node] = []

    class CreatableRepository:
        # org matches the local user's org — dedupe is now per-org
        nodes = _Nodes([_repo_node(uid="taken", slug="my-repo", org_uid="local-org")])  # forces slug dedup

        def __new__(cls, **kw):
            node = _repo_node(**kw)
            created.append(node)
            return node

    audits: list[dict] = []

    async def fake_audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(registration, "Repository", CreatableRepository)
    monkeypatch.setattr(registration, "write_audit", fake_audit)

    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"installation_id": 55, "owner": "ACME", "name": "my.repo"},  # case-insensitive
    )
    assert res.status_code == 201
    body = res.json()
    assert body["slug"] == "my-repo-2"  # deduped against the taken slug
    assert body["mode"] == "github"
    assert body["name"] == "My.Repo"
    assert body["github_owner"] == "acme"
    assert body["github_repo"] == "My.Repo"
    assert body["github_repo_id"] == 777
    assert body["github_installation_id"] == 55
    assert body["github_connection_status"] == "connected"
    assert body["default_branch"] == "develop"
    assert body["description"] == "live desc"

    assert len(created) == 1 and created[0].saved == 1
    assert [a["kind"] for a in audits] == ["repository.registered"]
    assert audits[0]["payload"]["installation_id"] == 55


# ── PAT-connection path (register + available-repos) ─────────────────────────


def _pat_conn(uid="conn-1", org_uid="local-org", account="octocat"):
    return SimpleNamespace(
        uid=uid,
        org_uid=org_uid,
        provider="github",
        kind="pat",
        external_id="pat:abc",
        display_name=account,
        created_at=None,
    )


def test_register_repo_via_pat_connection(monkeypatch, fake_installation_links):
    """No App required: the repo is verified against the token's live repo
    list, registered with git_connection_uid, and a webhook is attempted."""
    from domains.repositories.services import registration

    fake_installation_links._nodes.append(_pat_conn())
    monkeypatch.setattr(app_module, "connection_token", lambda conn: "ghp_x")

    async def fake_list(token):
        assert token == "ghp_x"
        return [_gh_repo(888, "acme", "svc", default_branch="dev", description="via token")]

    monkeypatch.setattr(app_module, "list_pat_repos", fake_list)

    hooks: list[dict] = []

    async def fake_hook(**kw):
        hooks.append(kw)
        return True

    monkeypatch.setattr(app_module, "maybe_create_repo_webhook", fake_hook)
    monkeypatch.setattr(app_module, "Repository", _fake_repository([]))

    created: list[_Node] = []

    class CreatableRepository:
        nodes = _Nodes([])

        def __new__(cls, **kw):
            node = _repo_node(**kw)
            created.append(node)
            return node

    async def fake_audit(**kw):
        pass

    monkeypatch.setattr(registration, "Repository", CreatableRepository)
    monkeypatch.setattr(registration, "write_audit", fake_audit)

    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"connection_uid": "conn-1", "owner": "ACME", "name": "svc"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["github_connection_status"] == "connected"
    assert body["github_installation_id"] is None
    assert body["default_branch"] == "dev"
    assert created[0].git_connection_uid == "conn-1"
    assert hooks == [{"token": "ghp_x", "owner": "acme", "name": "svc"}]


def test_register_repo_via_foreign_connection_is_404(monkeypatch, fake_installation_links):
    fake_installation_links._nodes.append(_pat_conn(org_uid="someone-else"))
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"connection_uid": "conn-1", "owner": "acme", "name": "svc"},
    )
    assert res.status_code == 404


def test_register_repo_requires_installation_or_connection():
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"owner": "acme", "name": "svc"},
    )
    assert res.status_code == 422


def test_register_repo_via_connection_with_undecryptable_token_is_409(
    monkeypatch, fake_installation_links
):
    fake_installation_links._nodes.append(_pat_conn())
    monkeypatch.setattr(app_module, "connection_token", lambda conn: "")
    res = TestClient(real_app).post(
        "/api/v1/github/app/register-repo",
        json={"connection_uid": "conn-1", "owner": "acme", "name": "svc"},
    )
    assert res.status_code == 409
    assert "re-add the token" in res.json()["detail"]


def test_available_repos_includes_pat_connection_groups(monkeypatch):
    """PAT groups appear (connection_uid set, id=0) even with no App at all."""

    async def one_connection(org_uid):
        return [_pat_conn()]

    monkeypatch.setattr(app_module, "org_pat_connections", one_connection)
    monkeypatch.setattr(app_module, "connection_token", lambda conn: "ghp_x")

    async def fake_list(token):
        return [_gh_repo(11, "acme", "api")]

    monkeypatch.setattr(app_module, "list_pat_repos", fake_list)
    monkeypatch.setattr(app_module, "Repository", _fake_repository([]))

    res = TestClient(real_app).get("/api/v1/github/app/available-repos")
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["install_url"] == ""  # no App configured
    [group] = body["installations"]
    assert group["connection_uid"] == "conn-1"
    assert group["id"] == 0
    assert group["account"] == "octocat"
    assert [r["full_name"] for r in group["repos"]] == ["acme/api"]


def test_available_repos_pat_group_surfaces_errors(monkeypatch):
    async def one_connection(org_uid):
        return [_pat_conn()]

    monkeypatch.setattr(app_module, "org_pat_connections", one_connection)
    monkeypatch.setattr(app_module, "connection_token", lambda conn: "ghp_x")

    async def boom(token):
        raise RuntimeError("token revoked")

    monkeypatch.setattr(app_module, "list_pat_repos", boom)
    monkeypatch.setattr(app_module, "Repository", _fake_repository([]))

    res = TestClient(real_app).get("/api/v1/github/app/available-repos")
    assert res.status_code == 200
    [group] = res.json()["installations"]
    assert "token revoked" in group["error"]
    assert group["repos"] == []
