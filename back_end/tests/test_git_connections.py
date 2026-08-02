"""Per-org PAT git connections — offline unit tests.

Covers: token fingerprint, seal/unseal roundtrip, PAT validation (mocked
httpx), create/idempotency/cross-org refusal (fake node store), env-token
auto-connect one-shot behavior, credential resolution precedence
(installation → connection PAT → env PAT), and the best-effort repo webhook
(URL gating + mocked httpx).
"""

import asyncio
from types import SimpleNamespace

import pytest

import domains.organizations.services.git_connections as svc
from config import settings
from infrastructure import github_app, secretbox

SECRETS_KEY = "unit-test-secrets-key-0123456789"


@pytest.fixture(autouse=True)
def reset_env_seed():
    svc._reset_env_seed_flag()
    yield
    svc._reset_env_seed_flag()


@pytest.fixture
def secrets_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", SECRETS_KEY, raising=False)
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", "", raising=False)
    secretbox._reset_cache()
    yield
    secretbox._reset_cache()


class _Nodes:
    def __init__(self, nodes=()):
        self._nodes = list(nodes)

    async def all(self):
        return list(self._nodes)

    async def filter(self, **kw):
        return [n for n in self._nodes if all(getattr(n, k, None) == v for k, v in kw.items())]

    async def get_or_none(self, **kw):
        matches = await self.filter(**kw)
        return matches[0] if matches else None


@pytest.fixture
def fake_connections(monkeypatch):
    """GitConnection without a DB — creations land in the same store."""
    store = _Nodes()

    class FakeGitConnection:
        nodes = store

        def __init__(self, **kw):
            defaults = dict(
                uid=f"conn-{len(store._nodes) + 1}",
                provider="github",
                kind="app",
                external_id="",
                display_name="",
                token_sealed="",
                org_uid="",
                linked_by="",
                created_at=None,
            )
            defaults.update(kw)
            self.__dict__.update(defaults)

        async def save(self):
            store._nodes.append(self)
            return self

        async def delete(self):
            store._nodes.remove(self)

    monkeypatch.setattr(svc, "GitConnection", FakeGitConnection)

    async def fake_audit(**kw):
        pass

    monkeypatch.setattr(svc, "write_audit", fake_audit)
    return store


# ── Fingerprint + sealing ────────────────────────────────────────────────────


def test_token_fingerprint_stable_and_opaque():
    fp = svc.token_fingerprint("ghp_secret")
    assert fp.startswith("pat:")
    assert "ghp_secret" not in fp
    assert fp == svc.token_fingerprint("ghp_secret")
    assert fp != svc.token_fingerprint("ghp_other")


def test_connection_token_roundtrip_sealed(secrets_key):
    conn = SimpleNamespace(uid="c1", token_sealed=svc._seal("ghp_x"))
    assert conn.token_sealed.startswith("enc:v2:")
    assert svc.connection_token(conn) == "ghp_x"


def test_connection_token_plaintext_without_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "", raising=False)
    secretbox._reset_cache()
    try:
        conn = SimpleNamespace(uid="c1", token_sealed=svc._seal("ghp_x"))
        assert conn.token_sealed == "ghp_x"  # dev without a key
        assert svc.connection_token(conn) == "ghp_x"
    finally:
        secretbox._reset_cache()


def test_connection_token_undecryptable_reads_empty(secrets_key, monkeypatch):
    conn = SimpleNamespace(uid="c1", token_sealed=svc._seal("ghp_x"))
    # Key rotates away → unseal fails → "" (repo falls back to env PAT).
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "a-completely-different-key-000", raising=False)
    secretbox._reset_cache()
    assert svc.connection_token(conn) == ""


# ── validate_pat (mocked httpx) ──────────────────────────────────────────────


def _fake_httpx_client(status_code, body=None):
    class FakeResponse:
        text = ""

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return body or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class FakeAsyncClient:
        def __init__(self, *a, **kw): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return FakeResponse()

        async def post(self, *a, **kw):
            return FakeResponse()

    return FakeAsyncClient


def test_validate_pat_returns_identity(monkeypatch):
    monkeypatch.setattr(svc.httpx, "AsyncClient", _fake_httpx_client(200, {"login": "octocat"}))
    assert asyncio.run(svc.validate_pat("ghp_x"))["login"] == "octocat"


def test_validate_pat_rejects_bad_token(monkeypatch):
    monkeypatch.setattr(svc.httpx, "AsyncClient", _fake_httpx_client(401))
    with pytest.raises(svc.PatValidationError):
        asyncio.run(svc.validate_pat("ghp_bad"))


# ── create_pat_connection ────────────────────────────────────────────────────


def _wire_identity(monkeypatch, login="octocat"):
    async def fake_validate(token):
        return {"login": login}

    monkeypatch.setattr(svc, "validate_pat", fake_validate)


def test_create_pat_connection_and_idempotency(monkeypatch, fake_connections):
    _wire_identity(monkeypatch)
    conn = asyncio.run(svc.create_pat_connection(org_uid="org-1", token="ghp_x", linked_by="u1"))
    assert conn.kind == "pat"
    assert conn.display_name == "octocat"
    assert conn.external_id == svc.token_fingerprint("ghp_x")
    assert svc.connection_token(conn) == "ghp_x"

    again = asyncio.run(svc.create_pat_connection(org_uid="org-1", token="ghp_x"))
    assert again is conn  # same org, same token → the existing connection
    assert len(fake_connections._nodes) == 1


def test_create_pat_connection_refuses_cross_org_token(monkeypatch, fake_connections):
    _wire_identity(monkeypatch)
    asyncio.run(svc.create_pat_connection(org_uid="org-1", token="ghp_x"))
    with pytest.raises(svc.PatValidationError, match="already connected"):
        asyncio.run(svc.create_pat_connection(org_uid="org-2", token="ghp_x"))


def test_create_pat_connection_rejects_empty_token(fake_connections):
    with pytest.raises(svc.PatValidationError):
        asyncio.run(svc.create_pat_connection(org_uid="org-1", token="   "))


def test_delete_pat_connection_org_scoped(monkeypatch, fake_connections):
    _wire_identity(monkeypatch)
    conn = asyncio.run(svc.create_pat_connection(org_uid="org-1", token="ghp_x"))
    assert asyncio.run(svc.delete_pat_connection(conn.uid, "org-OTHER")) is False
    assert asyncio.run(svc.delete_pat_connection(conn.uid, "org-1")) is True
    assert fake_connections._nodes == []


# ── Env-token auto-connect (one-shot) ────────────────────────────────────────


def test_ensure_env_pat_connection_connects_once(monkeypatch, fake_connections):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "ghp_env")
    asyncio.run(svc.ensure_env_pat_connection("org-1", linked_by="u1"))
    assert len(fake_connections._nodes) == 1
    assert fake_connections._nodes[0].org_uid == "org-1"
    # Settled: later logins (even other orgs) don't create anything.
    asyncio.run(svc.ensure_env_pat_connection("org-2"))
    assert len(fake_connections._nodes) == 1


def test_ensure_env_pat_connection_noop_without_token(monkeypatch, fake_connections):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
    asyncio.run(svc.ensure_env_pat_connection("org-1"))
    assert fake_connections._nodes == []


def test_ensure_env_pat_connection_respects_existing_connections(monkeypatch, fake_connections):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "ghp_env")
    fake_connections._nodes.append(SimpleNamespace(uid="existing", kind="app", org_uid="org-9"))
    asyncio.run(svc.ensure_env_pat_connection("org-1"))
    assert len(fake_connections._nodes) == 1  # untouched — the operator chose


def test_ensure_env_pat_connection_defers_on_network_error(monkeypatch, fake_connections):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "ghp_env")

    async def boom(token):
        raise OSError("github unreachable")

    monkeypatch.setattr(svc, "validate_pat", boom)
    asyncio.run(svc.ensure_env_pat_connection("org-1"))  # never raises
    assert fake_connections._nodes == []
    # Not settled → a later login retries and succeeds.
    _wire_identity(monkeypatch)
    asyncio.run(svc.ensure_env_pat_connection("org-1"))
    assert len(fake_connections._nodes) == 1


# ── Credential resolution precedence ─────────────────────────────────────────


def _repo(installation_id=None, connection_uid=None):
    return SimpleNamespace(
        github_installation_id=installation_id, git_connection_uid=connection_uid
    )


async def test_repo_token_prefers_connection_pat_over_env(monkeypatch):
    monkeypatch.setattr(github_app, "get_github_app", lambda: None)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "ghp_env")

    async def fake_conn_pat(uid):
        return "ghp_conn" if uid == "c1" else ""

    monkeypatch.setattr(github_app, "get_connection_pat", fake_conn_pat)
    assert await github_app.get_repo_git_token(_repo(connection_uid="c1")) == "ghp_conn"
    # Connection gone → env PAT.
    assert await github_app.get_repo_git_token(_repo(connection_uid="gone")) == "ghp_env"
    # No connection at all → env PAT (unchanged legacy behavior).
    assert await github_app.get_repo_git_token(_repo()) == "ghp_env"


async def test_repo_token_installation_beats_connection(monkeypatch):
    from infrastructure import github_app_store

    monkeypatch.setattr(
        github_app, "get_github_app",
        lambda: github_app_store.GitHubAppConfig(app_id="1", pem="x"),
    )

    async def fake_token(installation_id):
        return "ghs_inst"

    monkeypatch.setattr(github_app, "get_installation_token", fake_token)
    assert (
        await github_app.get_repo_git_token(_repo(installation_id=5, connection_uid="c1"))
        == "ghs_inst"
    )


async def test_repo_token_mint_failure_falls_back_to_connection_pat(monkeypatch):
    from infrastructure import github_app_store

    monkeypatch.setattr(
        github_app, "get_github_app",
        lambda: github_app_store.GitHubAppConfig(app_id="1", pem="x"),
    )

    async def boom(installation_id):
        raise RuntimeError("github down")

    monkeypatch.setattr(github_app, "get_installation_token", boom)

    async def fake_conn_pat(uid):
        return "ghp_conn"

    monkeypatch.setattr(github_app, "get_connection_pat", fake_conn_pat)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
    assert (
        await github_app.get_repo_git_token(_repo(installation_id=5, connection_uid="c1"))
        == "ghp_conn"
    )


async def test_repository_dto_carries_connection_uid_to_token_resolution(monkeypatch):
    """Regression: sandbox clones resolve credentials off a RepositoryDTO, not
    the node. The DTO dropping git_connection_uid made PAT-connection repos
    fail with "no GitHub credential" on deployments without an env PAT."""
    from domains.repositories.services.repository_service import repository_to_dto

    node = SimpleNamespace(
        uid="r1", org_uid="org-1", slug="repo", mode="github", provider="github",
        name="repo", description="", default_branch="main", color_scheme="indigo",
        is_active=True, github_owner="acme", github_repo="repo", github_repo_id=1,
        github_installation_id=None, git_connection_uid="c1",
        github_connection_status="connected", last_synced_at=None, metadata={},
        kill_switch_active=False, created_at=None, updated_at=None,
    )
    dto = repository_to_dto(node)
    assert dto.git_connection_uid == "c1"

    monkeypatch.setattr(github_app, "get_github_app", lambda: None)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")

    async def fake_conn_pat(uid):
        return "ghp_conn" if uid == "c1" else ""

    monkeypatch.setattr(github_app, "get_connection_pat", fake_conn_pat)
    assert await github_app.get_repo_git_token(dto) == "ghp_conn"


# ── Repo webhook (best-effort) ───────────────────────────────────────────────


def test_webhook_url_gating(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "s3cret")
    assert svc._webhook_url() == ""  # local origin — GitHub can't reach it

    monkeypatch.setattr(settings, "OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    assert svc._webhook_url() == ""  # no secret — receiver fails closed

    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "s3cret")
    assert svc._webhook_url() == "https://app.example.com/api/v1/github/webhook"


def test_maybe_create_repo_webhook_created_and_tolerated(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "s3cret")

    monkeypatch.setattr(svc.httpx, "AsyncClient", _fake_httpx_client(201))
    assert asyncio.run(svc.maybe_create_repo_webhook(token="t", owner="a", name="r")) is True

    for status in (422, 403, 404):  # exists / no permission — never raises
        monkeypatch.setattr(svc.httpx, "AsyncClient", _fake_httpx_client(status))
        assert asyncio.run(svc.maybe_create_repo_webhook(token="t", owner="a", name="r")) is False


def test_maybe_create_repo_webhook_skips_local_origin(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL", "http://localhost:8001")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "s3cret")

    def explode(*a, **kw):
        raise AssertionError("no HTTP call expected for a local origin")

    monkeypatch.setattr(svc.httpx, "AsyncClient", explode)
    assert asyncio.run(svc.maybe_create_repo_webhook(token="t", owner="a", name="r")) is False
