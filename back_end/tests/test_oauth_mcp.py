"""OAuth gateway for `opensweep connect` — pure logic + mounted surface.

PKCE verification, scope normalization, redirect-uri matching (exact +
RFC 8252 loopback port variance), token prefixes/hashing, and the public
endpoint + metadata surface.
"""

import base64
import hashlib

import pytest
from fastapi import HTTPException

from app import app
from domains.oauth_mcp.services.oauth_service import (
    ACCESS_TOKEN_PREFIX,
    CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    hash_secret,
    mint_secret,
    normalize_scope,
    redirect_uri_allowed,
    scope_allows_write,
    validate_redirect_uri_scheme,
    verify_pkce_s256,
)


# ── PKCE ─────────────────────────────────────────────────────────────────────


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )


def test_pkce_s256_roundtrip():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert verify_pkce_s256(verifier, _challenge(verifier))


def test_pkce_rejects_wrong_verifier():
    assert not verify_pkce_s256("wrong", _challenge("right"))


def test_pkce_rejects_empty():
    assert not verify_pkce_s256("", "")
    assert not verify_pkce_s256("v", "")


# ── Scopes ───────────────────────────────────────────────────────────────────


def test_scope_defaults_to_read():
    assert normalize_scope("") == "mcp:read"


def test_scope_accepts_known_and_dedupes():
    assert normalize_scope("mcp:read mcp:write mcp:read") == "mcp:read mcp:write"


def test_scope_rejects_unknown():
    with pytest.raises(HTTPException) as exc:
        normalize_scope("mcp:read admin:everything")
    assert exc.value.status_code == 400


def test_scope_write_predicate():
    assert scope_allows_write("mcp:read mcp:write")
    assert not scope_allows_write("mcp:read")


# ── Redirect URIs ────────────────────────────────────────────────────────────


def test_redirect_exact_match():
    assert redirect_uri_allowed("https://x/cb", ["https://x/cb"])
    assert not redirect_uri_allowed("https://evil/cb", ["https://x/cb"])


def test_redirect_loopback_port_variance():
    # RFC 8252 §7.3: loopback clients bind an ephemeral port per run.
    assert redirect_uri_allowed(
        "http://127.0.0.1:53171/callback", ["http://127.0.0.1:41999/callback"]
    )
    assert not redirect_uri_allowed(
        "http://127.0.0.1:53171/other", ["http://127.0.0.1:41999/callback"]
    )
    assert not redirect_uri_allowed(
        "http://10.0.0.5:53171/callback", ["http://10.0.0.5:41999/callback"]
    )


# ── Token shape ──────────────────────────────────────────────────────────────


def test_token_prefixes_are_distinct():
    assert len({ACCESS_TOKEN_PREFIX, REFRESH_TOKEN_PREFIX, CODE_PREFIX}) == 3


def test_minted_secrets_are_unique_and_prefixed():
    a, b = mint_secret(ACCESS_TOKEN_PREFIX), mint_secret(ACCESS_TOKEN_PREFIX)
    assert a != b and a.startswith(ACCESS_TOKEN_PREFIX)


def test_hash_is_stable_and_not_identity():
    assert hash_secret("x") == hash_secret("x")
    assert hash_secret("x") != "x"


# ── Mounted surface ──────────────────────────────────────────────────────────


def test_gateway_routes_are_mounted():
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/.well-known/oauth-protected-resource" in paths
    assert "/.well-known/oauth-authorization-server" in paths
    assert "/oauth/register" in paths
    assert "/oauth/authorize" in paths
    assert "/oauth/token" in paths
    assert "/api/v1/oauth-mcp/approve" in paths


def test_public_endpoints_are_auth_exempt():
    from app import TokenAuthMiddleware

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/oauth/register",
        "/oauth/authorize",
        "/oauth/token",
    ):
        assert path in TokenAuthMiddleware.EXEMPT_PATHS, path
    # The consent + metadata endpoints must NOT be exempt — they need the
    # logged-in user (the metadata endpoint feeds the trusted client-name
    # display, so it has to be gated too — anonymous callers could fingerprint
    # every registered client_id otherwise).
    assert "/api/v1/oauth-mcp/approve" not in TokenAuthMiddleware.EXEMPT_PATHS
    assert "/api/v1/oauth-mcp/client_metadata" not in TokenAuthMiddleware.EXEMPT_PATHS


def test_client_metadata_route_is_mounted():
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/oauth-mcp/client_metadata" in paths


# ── Redirect URI scheme hardening ────────────────────────────────────────────
#
# The old register endpoint accepted any string as a redirect_uri — including
# `javascript:`, `data:`, `file:`, and empty-scheme URIs. Even though
# redirect_uri_allowed() would exact-match them, once matched they got fed to
# a browser RedirectResponse, and a `javascript:` URL a browser follows from
# our origin is XSS. Everything below pins the scheme allow-list.


def test_register_rejects_javascript_scheme():
    with pytest.raises(HTTPException) as exc:
        validate_redirect_uri_scheme("javascript:alert(1)")
    assert exc.value.status_code == 400


def test_register_rejects_data_and_file_schemes():
    for uri in ("data:text/html,<script>1</script>", "file:///etc/passwd"):
        with pytest.raises(HTTPException):
            validate_redirect_uri_scheme(uri)


def test_register_rejects_public_http():
    # Public-internet http (as opposed to https) is not allowed — cleartext
    # code delivery, plus it's a favorite in phishing-adjacent OAuth abuse.
    with pytest.raises(HTTPException):
        validate_redirect_uri_scheme("http://example.com/cb")


def test_register_rejects_empty_and_bare_schemes():
    for uri in ("", "cb", "://cb", "mailto:x@y"):
        with pytest.raises(HTTPException):
            validate_redirect_uri_scheme(uri)


def test_register_accepts_https():
    validate_redirect_uri_scheme("https://example.com/cb")  # no raise


def test_register_accepts_http_loopback():
    for uri in (
        "http://127.0.0.1:8080/cb",
        "http://localhost/cb",
        "http://[::1]:1234/cb",
    ):
        validate_redirect_uri_scheme(uri)  # no raise


def test_register_accepts_custom_scheme_with_dot():
    # RFC 8252 §7.1 reverse-DNS custom schemes.
    validate_redirect_uri_scheme("com.example.app:/oauth/callback")
    validate_redirect_uri_scheme("io.opensweep.cli:/cb")


# ── Single-use invariants (concurrent-redemption race) ───────────────────────
#
# The exchange_code + refresh_tokens flows both used to be read-check-then-
# save on the OAuthCode/OAuthToken node, which let two concurrent callers
# both pass the "used_at is None" / "revoked_at is None" guard before either
# wrote. Both would mint token pairs from one code / refresh. These pin the
# CAS behavior so a regression brings them back.


def _asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_exchange_code_is_a_compare_and_swap(monkeypatch):
    """Second exchange of the same code MUST get invalid_grant. The first
    exchange should also drive the atomic UPDATE — if two callers both saw
    used_at IS NULL, only one Cypher UPDATE returns a row."""
    from types import SimpleNamespace

    from domains.oauth_mcp.services import oauth_service

    calls: list[dict] = []
    stored = {"used_at": None}

    async def fake_cypher(query: str, params: dict):
        calls.append({"q": query, "p": dict(params)})
        assert "WHERE n.used_at IS NULL" in query, (
            "code exchange must claim atomically via a WHERE-clause CAS"
        )
        assert "SET n.used_at" in query
        if stored["used_at"] is not None:
            return [], None
        stored["used_at"] = params["now"]
        node_shape = SimpleNamespace(
            expires_at=None,
            client_id="c-1",
            user_uid="u-1",
            scope="mcp:read",
            code_challenge=_challenge("verifier-123"),
            redirect_uri="https://x/cb",
        )
        return [[node_shape]], None

    class _FakeAdb:
        async def cypher_query(self, q, p=None):
            return await fake_cypher(q, p or {})

    monkeypatch.setattr(oauth_service, "adb", _FakeAdb())
    monkeypatch.setattr(
        oauth_service, "OAuthCode",
        SimpleNamespace(inflate=lambda n: n),
    )

    async def _mint(**kw):
        return SimpleNamespace(_access_token="a", _refresh_token="r", scope=kw.get("scope"))

    monkeypatch.setattr(oauth_service, "_mint_token_pair", _mint)

    # First exchange wins.
    tok = _asyncio_run(oauth_service.exchange_code(
        code="osmcc_x", client_id="c-1", redirect_uri="https://x/cb", code_verifier="verifier-123",
    ))
    assert tok._access_token == "a"
    # Second exchange (same code) is refused — the CAS returns zero rows.
    with pytest.raises(HTTPException) as exc:
        _asyncio_run(oauth_service.exchange_code(
            code="osmcc_x", client_id="c-1", redirect_uri="https://x/cb", code_verifier="verifier-123",
        ))
    assert exc.value.status_code == 400
    assert len(calls) == 2  # both attempts hit the CAS


def test_refresh_atomic_rotate_rejects_concurrent_second(monkeypatch):
    """If a concurrent refresh already rotated the node (CAS returns no
    rows), the loser must delete its orphan successor and treat the
    presented token as reused."""
    from types import SimpleNamespace

    from domains.oauth_mcp.services import oauth_service

    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)

    class _Successor:
        def __init__(self, uid):
            self.uid = uid
            self.deleted = False

        async def delete(self):
            self.deleted = True

    successor = _Successor("t-successor")

    async def _mint(**kw):
        return successor

    monkeypatch.setattr(oauth_service, "_mint_token_pair", _mint)

    node = SimpleNamespace(
        uid="t-1",
        client_id="c-1",
        user_uid="u-1",
        scope="mcp:read",
        revoked_at=None,
        rotated_to="",
        refresh_expires_at=now + timedelta(days=1),
    )

    class _Nodes:
        async def get_or_none(self, **kw):
            return node  # both refresh_hash lookup and uid lookup

    monkeypatch.setattr(oauth_service, "OAuthToken", SimpleNamespace(nodes=_Nodes()))

    audits: list[dict] = []

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(oauth_service, "write_audit", _audit)

    async def _fam(n):
        n.revoked_at = now
        return 1

    monkeypatch.setattr(oauth_service, "revoke_token_family", _fam)

    class _AdbLoser:
        async def cypher_query(self, q, p=None):
            # Simulate concurrent winner: WHERE revoked_at IS NULL matched
            # nothing because someone else already rotated.
            assert "WHERE n.revoked_at IS NULL" in q
            return [], None

    monkeypatch.setattr(oauth_service, "adb", _AdbLoser())

    with pytest.raises(HTTPException) as exc:
        _asyncio_run(oauth_service.refresh_tokens(refresh_token="osmcr_x", client_id="c-1"))
    assert exc.value.status_code == 400
    assert successor.deleted, "orphan successor must be cleaned up when CAS loses"
    assert any(a["kind"] == "oauth_token.reuse_detected" for a in audits)


def test_refresh_cross_client_is_audited(monkeypatch):
    """A live refresh token presented under the wrong client_id is not
    reuse-of-revoked (so no family kill) — but it IS suspicious enough to
    audit, either as client confusion or a stolen-token replay."""
    from types import SimpleNamespace

    from domains.oauth_mcp.services import oauth_service

    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    node = SimpleNamespace(
        uid="t-1",
        client_id="c-bound",
        user_uid="u-1",
        scope="mcp:read",
        revoked_at=None,
        rotated_to="",
        refresh_expires_at=now + timedelta(days=1),
    )

    class _Nodes:
        async def get_or_none(self, **kw):
            return node

    monkeypatch.setattr(oauth_service, "OAuthToken", SimpleNamespace(nodes=_Nodes()))

    audits: list[dict] = []

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(oauth_service, "write_audit", _audit)

    with pytest.raises(HTTPException) as exc:
        _asyncio_run(
            oauth_service.refresh_tokens(refresh_token="osmcr_x", client_id="c-other")
        )
    assert exc.value.status_code == 400
    assert audits == [{
        "kind": "oauth_token.cross_client_refresh",
        "subject_uid": "t-1",
        "subject_type": "OAuthToken",
        "actor_uid": "u-1",
        "payload": {"client_id_bound": "c-bound", "client_id_presented": "c-other"},
    }]
    # Live token was NOT revoked — no family kill for a mismatched client_id.
    assert node.revoked_at is None
