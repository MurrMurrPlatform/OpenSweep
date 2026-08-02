"""Redis-backed single-use state nonces + Redis L2 installation-token cache.

Reuses test_github_app's autouse `isolated_app_store` fixture, which yields a
per-test FakeAsyncRedis (patched into infrastructure.redis_client) beside the
usual tmp store isolation.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api.v1.github_app as app_module
from api.v1.github_app import (
    INSTALL_STATE_MAX_AGE_SECONDS,
    mint_install_state,
)
from app import app as real_app
from config import settings
from infrastructure import github_app, github_app_store, state_nonce_store
from tests.test_github_app import isolated_app_store  # noqa: F401  (autouse fixture)

# ── state_nonce_store: Redis-only primitives ─────────────────────────────────


async def test_nonce_remember_then_consume_exactly_once(isolated_app_store):  # noqa: F811
    await state_nonce_store.remember_state_nonce("kas_abc", 600)
    assert await state_nonce_store.consume_state_nonce("kas_abc") is True
    assert await state_nonce_store.consume_state_nonce("kas_abc") is False


async def test_nonce_distinct_states_are_independent(isolated_app_store):  # noqa: F811
    await state_nonce_store.remember_state_nonce("kas_one", 600)
    await state_nonce_store.remember_state_nonce("kis_two", 3600)
    assert await state_nonce_store.consume_state_nonce("kas_one") is True
    assert await state_nonce_store.consume_state_nonce("kis_two") is True
    assert await state_nonce_store.consume_state_nonce("kas_one") is False


async def test_nonce_keys_are_hashed_with_expected_ttls(isolated_app_store):  # noqa: F811
    fake = isolated_app_store
    await state_nonce_store.remember_state_nonce("sls_slack", 600)
    await state_nonce_store.remember_state_nonce("kis_install", INSTALL_STATE_MAX_AGE_SECONDS)
    assert len(fake.store) == 2
    for key in fake.store:
        assert key.startswith("opensweep:ghapp:state:")
        assert "sls_" not in key and "kis_" not in key  # raw token never stored
    assert sorted(fake.ex_by_key.values()) == [600, 3600]


async def test_install_url_mints_and_remembers_with_hour_ttl(isolated_app_store):  # noqa: F811
    fake = isolated_app_store
    url = await app_module._install_url(SimpleNamespace(slug="opensweep-x"), "org-1")
    assert "?state=kis_" in url
    assert list(fake.ex_by_key.values()) == [INSTALL_STATE_MAX_AGE_SECONDS]
    # No org → plain URL, nothing remembered.
    assert (
        await app_module._install_url(SimpleNamespace(slug="opensweep-x"))
        == "https://github.com/apps/opensweep-x/installations/new"
    )
    assert len(fake.store) == 1


# ── remember/consume: Redis-first with file-ledger fallback ──────────────────


async def test_redis_down_falls_back_to_file_ledger(isolated_app_store):  # noqa: F811
    fake = isolated_app_store
    fake.raise_exc = ConnectionError("redis down")
    state = mint_install_state("org-1")
    await app_module.remember_state(state, INSTALL_STATE_MAX_AGE_SECONDS)
    assert not fake.store  # nothing reached Redis
    assert await app_module.consume_state(state) is True
    assert await app_module.consume_state(state) is False


async def test_file_only_state_still_consumes_with_healthy_redis(isolated_app_store):  # noqa: F811
    # Rolling-deploy bridge: a state minted by a pre-upgrade process lives only
    # in the file ledger — consume must still find it.
    state = mint_install_state("org-1")
    app_module._file_remember_state(state)
    assert await app_module.consume_state(state) is True
    assert await app_module.consume_state(state) is False


def test_setup_rejects_replayed_install_state(isolated_app_store):  # noqa: F811
    state = mint_install_state("org-1")
    asyncio.run(app_module.remember_state(state, INSTALL_STATE_MAX_AGE_SECONDS))
    client = TestClient(real_app)
    first = client.get(f"/api/v1/github/app/setup?state={state}", follow_redirects=False)
    assert first.status_code == 302
    assert "install_error" not in first.headers["location"]
    # Replay: no 403 dead-end (could be a pre-upgrade state or a double-click) —
    # redirect back so the user can retry with a freshly minted state. No link
    # happens on this path.
    replay = client.get(f"/api/v1/github/app/setup?state={state}", follow_redirects=False)
    assert replay.status_code == 302
    assert "install_error=state_reused" in replay.headers["location"]


def test_setup_never_remembered_install_state_redirects_without_link(isolated_app_store):  # noqa: F811
    # Signature-valid but never remembered (e.g. best-effort remember failed
    # everywhere, or minted pre-upgrade) → retry redirect, not a link.
    state = mint_install_state("org-1")
    res = TestClient(real_app).get(
        f"/api/v1/github/app/setup?state={state}", follow_redirects=False
    )
    assert res.status_code == 302
    assert "install_error=state_reused" in res.headers["location"]


# ── Installation token cache: Redis L2 ──────────────────────────────────────


def _wire_app(monkeypatch, minted, *, delay: float = 0.0, expires_in=timedelta(hours=1)):
    async def fake_request(app, installation_id):
        minted.append(installation_id)
        if delay:
            await asyncio.sleep(delay)
        return {
            "token": f"ghs_tok{len(minted)}",
            "expires_at": (datetime.now(UTC) + expires_in).isoformat(),
        }

    monkeypatch.setattr(github_app, "_request_installation_token", fake_request)
    monkeypatch.setattr(
        github_app, "get_github_app",
        lambda: github_app_store.GitHubAppConfig(app_id="1", pem="x"),
    )


async def test_fresh_process_reads_token_from_redis(isolated_app_store, monkeypatch):  # noqa: F811
    minted: list[int] = []
    _wire_app(monkeypatch, minted)

    t1 = await github_app.get_installation_token(5)
    assert minted == [5]
    # "New process": L1 gone, same Redis.
    github_app._token_cache.clear()
    t2 = await github_app.get_installation_token(5)
    assert t2 == t1 == "ghs_tok1"
    assert minted == [5]  # served from Redis — no second mint
    assert github_app._token_cache[5].token == "ghs_tok1"  # L1 repopulated


async def test_concurrent_callers_single_flight_one_mint(isolated_app_store, monkeypatch):  # noqa: F811
    minted: list[int] = []
    _wire_app(monkeypatch, minted, delay=0.05)

    tokens = await asyncio.gather(*[github_app.get_installation_token(9) for _ in range(10)])
    assert set(tokens) == {"ghs_tok1"}
    assert minted == [9]  # exactly one mint across all 10 callers


async def test_redis_entry_within_refresh_margin_is_reminted(isolated_app_store, monkeypatch):  # noqa: F811
    minted: list[int] = []
    _wire_app(monkeypatch, minted)

    stale = github_app._CachedToken(
        token="ghs_stale", expires_at=datetime.now(UTC) + timedelta(minutes=4)
    )
    await github_app._redis_write_token(7, stale)
    assert await github_app.get_installation_token(7) == "ghs_tok1"
    assert minted == [7]


async def test_redis_down_degrades_to_local_mint(isolated_app_store, monkeypatch):  # noqa: F811
    fake = isolated_app_store
    fake.raise_exc = ConnectionError("redis down")
    minted: list[int] = []
    _wire_app(monkeypatch, minted)

    assert await github_app.get_installation_token(3) == "ghs_tok1"
    assert minted == [3]
    # L1 still serves while Redis stays down.
    assert await github_app.get_installation_token(3) == "ghs_tok1"
    assert minted == [3]


async def test_clear_token_cache_sweeps_redis_keys(isolated_app_store, monkeypatch):  # noqa: F811
    fake = isolated_app_store
    minted: list[int] = []
    _wire_app(monkeypatch, minted)

    await github_app.get_installation_token(4)
    fake.store["opensweep:ghapp:mint_lock:99"] = "leftover"  # a wedged lock too
    assert any(k.startswith("opensweep:ghapp:inst_token:") for k in fake.store)

    await github_app.clear_token_cache()
    assert not github_app._token_cache
    assert not any(k.startswith("opensweep:ghapp:inst_token:") for k in fake.store)
    assert not any(k.startswith("opensweep:ghapp:mint_lock:") for k in fake.store)


async def test_redis_token_sealed_at_rest_when_key_configured(isolated_app_store, monkeypatch):  # noqa: F811
    from infrastructure import secretbox

    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "unit-test-secrets-key-0123456789", raising=False)
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", "", raising=False)
    secretbox._reset_cache()
    try:
        fake = isolated_app_store
        minted: list[int] = []
        _wire_app(monkeypatch, minted)

        t1 = await github_app.get_installation_token(6)
        raw = fake.store["opensweep:ghapp:inst_token:6"]
        assert raw.startswith("enc:v2:")
        assert "ghs_tok1" not in raw

        github_app._token_cache.clear()
        assert await github_app.get_installation_token(6) == t1
        assert minted == [6]
    finally:
        secretbox._reset_cache()
