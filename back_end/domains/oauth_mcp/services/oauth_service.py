"""OAuth gateway logic — pure helpers + node lifecycle.

Pure parts (PKCE verification, token minting/hashing, scope parsing,
redirect-uri matching) carry the tests; the async service functions are thin
node plumbing around them.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from neomodel import adb

from domains.oauth_mcp.models import OAUTH_SCOPES, OAuthClient, OAuthCode, OAuthToken
from infrastructure.audit import write_audit

ACCESS_TOKEN_PREFIX = "osmcp_"
REFRESH_TOKEN_PREFIX = "osmcr_"
CODE_PREFIX = "osmcc_"

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=10)

DEFAULT_SCOPE = "mcp:read"


# ── Pure helpers ─────────────────────────────────────────────────────────────


def hash_secret(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


def mint_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)) == challenge."""
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii", errors="replace")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, challenge)


def normalize_scope(requested: str) -> str:
    """Intersect the request with the supported scopes; empty → default.
    Unknown scopes are rejected, not silently dropped, so a client asking for
    more than we grant finds out at authorize time."""
    parts = [s for s in (requested or "").split() if s]
    if not parts:
        return DEFAULT_SCOPE
    unknown = [s for s in parts if s not in OAUTH_SCOPES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported scope: {' '.join(unknown)}")
    return " ".join(dict.fromkeys(parts))


def redirect_uri_allowed(redirect_uri: str, registered: list[str]) -> bool:
    """Exact-match against the registered list (OAuth 2.1 — no wildcards).
    Loopback redirects (RFC 8252 §7.3) may vary the PORT only."""
    if not redirect_uri:
        return False
    if redirect_uri in registered:
        return True
    from urllib.parse import urlparse

    try:
        presented = urlparse(redirect_uri)
    except ValueError:
        return False
    if presented.hostname not in {"127.0.0.1", "::1", "localhost"}:
        return False
    for r in registered:
        try:
            reg = urlparse(r)
        except ValueError:
            continue
        if (
            reg.hostname == presented.hostname
            and reg.scheme == presented.scheme
            and reg.path == presented.path
        ):
            return True
    return False


def validate_redirect_uri_scheme(redirect_uri: str) -> None:
    """Reject register-time redirect_uris that a client-side redirect could
    turn into script or filesystem access. OAuth 2.1 §5.2.1 + RFC 8252 pin the
    acceptable shapes for public clients to:

      - `https://…` (production web callbacks),
      - `http://` on loopback (`127.0.0.1`, `::1`, `localhost`) for native
        apps that bind an ephemeral port per RFC 8252 §7.3,
      - a custom scheme whose *scheme* contains a dot — the reverse-DNS
        pattern the same RFC recommends (§7.1), e.g. `com.example.app:/cb`.

    Everything else — `javascript:`, `data:`, `file:`, bare `http://` on the
    public internet, empty scheme — is refused up front so the consent
    screen cannot later be handed a URL a browser will treat as script.
    Raises HTTPException(400) with an OAuth-shaped error message on failure.
    """
    from urllib.parse import urlparse

    if not redirect_uri:
        raise HTTPException(status_code=400, detail="invalid_redirect_uri: empty")
    try:
        parsed = urlparse(redirect_uri)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid_redirect_uri: unparseable ({exc})"
        ) from None
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise HTTPException(status_code=400, detail="invalid_redirect_uri: missing scheme")
    if scheme == "https":
        if not parsed.hostname:
            raise HTTPException(
                status_code=400, detail="invalid_redirect_uri: https requires host"
            )
        return
    if scheme == "http":
        if (parsed.hostname or "").lower() not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(
                status_code=400,
                detail="invalid_redirect_uri: http is only allowed on loopback "
                "(127.0.0.1, ::1, localhost)",
            )
        return
    # Custom scheme (RFC 8252 §7.1): reverse-DNS pattern requires a dot in the
    # scheme so we can distinguish `com.example.app` from `javascript`, `data`,
    # `file`, `mailto`, `chrome-extension`, and other browser-executable or
    # side-effecting schemes.
    if "." in scheme:
        return
    raise HTTPException(
        status_code=400,
        detail=f"invalid_redirect_uri: unsupported scheme '{scheme}' "
        "(use https, http on loopback, or a reverse-DNS custom scheme)",
    )


def scope_allows_write(scope: str) -> bool:
    return "mcp:write" in (scope or "").split()


# ── Node lifecycle ───────────────────────────────────────────────────────────


async def register_client(*, name: str, redirect_uris: list[str]) -> OAuthClient:
    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")
    kept = redirect_uris[:10]
    for uri in kept:
        validate_redirect_uri_scheme(uri)
    client = OAuthClient(
        uid=uuid4().hex, name=(name or "")[:120], redirect_uris=kept
    )
    await client.save()
    await write_audit(
        kind="oauth_client.registered",
        subject_uid=client.uid,
        subject_type="OAuthClient",
        actor_uid="anonymous",
        payload={"name": client.name, "redirect_uris": redirect_uris[:10]},
    )
    return client


async def get_client(client_id: str) -> OAuthClient:
    client = await OAuthClient.nodes.get_or_none(uid=(client_id or "").strip())
    if client is None:
        raise HTTPException(status_code=400, detail="unknown client_id")
    return client


async def issue_code(
    *,
    client: OAuthClient,
    user_uid: str,
    scope: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    code = mint_secret(CODE_PREFIX)
    now = datetime.now(UTC)
    node = OAuthCode(
        uid=uuid4().hex,
        code_hash=hash_secret(code),
        client_id=client.uid,
        user_uid=user_uid,
        scope=scope,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        expires_at=now + CODE_TTL,
    )
    await node.save()
    return code


async def exchange_code(
    *, code: str, client_id: str, redirect_uri: str, code_verifier: str
) -> OAuthToken:
    """Trade an auth code for a token pair.

    The claim is a Cypher compare-and-swap (`WHERE n.used_at IS NULL`) so two
    concurrent exchanges of one code cannot both succeed — OAuth 2.1 §4.1.2
    requires an authorization code to be single-use, even under retry storms.
    A read-check-then-save flow would let both readers see `used_at=None`
    before either wrote, and both would mint a token pair from the same code.
    Post-claim we still validate the bindings (client_id, redirect_uri, PKCE,
    expiry) — a failure there burns the code, which is the spec-mandated
    behavior on any attempted exchange.
    """
    now = datetime.now(UTC)
    rows, _ = await adb.cypher_query(
        "MATCH (n:OAuthCode {code_hash: $ch}) "
        "WHERE n.used_at IS NULL "
        "SET n.used_at = $now "
        "RETURN n",
        {"ch": hash_secret(code or ""), "now": now},
    )
    if not rows:
        raise HTTPException(status_code=400, detail="invalid_grant")
    node = OAuthCode.inflate(rows[0][0])
    if (
        (node.expires_at and node.expires_at < now)
        or node.client_id != (client_id or "").strip()
        or node.redirect_uri != (redirect_uri or "")
        or not verify_pkce_s256(code_verifier, node.code_challenge)
    ):
        raise HTTPException(status_code=400, detail="invalid_grant")
    return await _mint_token_pair(
        client_id=node.client_id, user_uid=node.user_uid, scope=node.scope
    )


async def revoke_token_family(node: OAuthToken) -> int:
    """Revoke a token and every successor minted from it (walk `rotated_to`).
    Called on refresh-token REUSE: a rotated token being presented again
    means the client or an attacker holds a stolen copy — per OAuth 2.1 /
    RFC 9700 the whole family dies, so the stolen successor dies with it."""
    now = datetime.now(UTC)
    revoked = 0
    seen: set[str] = set()
    current: OAuthToken | None = node
    while current is not None and current.uid not in seen:
        seen.add(current.uid)
        if current.revoked_at is None:
            current.revoked_at = now
            await current.save()
            revoked += 1
        next_uid = (current.rotated_to or "").strip()
        current = await OAuthToken.nodes.get_or_none(uid=next_uid) if next_uid else None
    return revoked


async def refresh_tokens(*, refresh_token: str, client_id: str) -> OAuthToken:
    node = await OAuthToken.nodes.get_or_none(refresh_hash=hash_secret(refresh_token or ""))
    now = datetime.now(UTC)
    if node is not None and node.revoked_at is not None:
        # Reuse of a rotated/revoked refresh token — kill the whole family.
        revoked = await revoke_token_family(node)
        await write_audit(
            kind="oauth_token.reuse_detected",
            subject_uid=node.uid,
            subject_type="OAuthToken",
            actor_uid=node.user_uid,
            payload={"client_id": node.client_id, "descendants_revoked": revoked},
        )
        raise HTTPException(status_code=400, detail="invalid_grant")
    if node is None:
        raise HTTPException(status_code=400, detail="invalid_grant")
    presented_client = (client_id or "").strip()
    if node.client_id != presented_client:
        # A refresh token bound to one client presented under another's
        # client_id is either client confusion or a stolen-token replay.
        # The RFC 9700 "kill the family" rule only fires on reuse of a
        # REVOKED token, so we don't tear down this user's live sessions —
        # but we do want the mismatch on the audit trail so operators can
        # spot a client leaking credentials into another integration.
        await write_audit(
            kind="oauth_token.cross_client_refresh",
            subject_uid=node.uid,
            subject_type="OAuthToken",
            actor_uid=node.user_uid,
            payload={
                "client_id_bound": node.client_id,
                "client_id_presented": presented_client,
            },
        )
        raise HTTPException(status_code=400, detail="invalid_grant")
    if node.refresh_expires_at and node.refresh_expires_at < now:
        raise HTTPException(status_code=400, detail="invalid_grant")
    successor = await _mint_token_pair(
        client_id=node.client_id, user_uid=node.user_uid, scope=node.scope
    )
    # Atomic rotate: `WHERE n.revoked_at IS NULL` is a compare-and-swap that
    # prevents two concurrent refreshes from both minting a successor from
    # the same refresh token (OAuth 2.1 §6.1 single-use rotation). A
    # read-then-write flow would let both callers pass the earlier
    # `revoked_at is None` guard before either saved, and both would return
    # a token pair from the same refresh — undetectable as reuse afterward
    # because both are "the successor".
    rows, _ = await adb.cypher_query(
        "MATCH (n:OAuthToken {uid: $uid}) "
        "WHERE n.revoked_at IS NULL "
        "SET n.revoked_at = $now, n.rotated_to = $succ "
        "RETURN n.uid",
        {"uid": node.uid, "now": now, "succ": successor.uid},
    )
    if not rows:
        # A concurrent refresh already rotated `node`. From this caller's
        # perspective the presented refresh token is now revoked, which is
        # RFC 9700's reuse condition — kill the family. Clean up our orphan
        # successor first so we don't leave an unrooted live token behind.
        await successor.delete()
        node_now = await OAuthToken.nodes.get_or_none(uid=node.uid)
        if node_now is not None:
            revoked = await revoke_token_family(node_now)
            await write_audit(
                kind="oauth_token.reuse_detected",
                subject_uid=node_now.uid,
                subject_type="OAuthToken",
                actor_uid=node_now.user_uid,
                payload={
                    "client_id": node_now.client_id,
                    "descendants_revoked": revoked,
                    "race": True,
                },
            )
        raise HTTPException(status_code=400, detail="invalid_grant")
    return successor


async def _mint_token_pair(*, client_id: str, user_uid: str, scope: str) -> OAuthToken:
    now = datetime.now(UTC)
    access = mint_secret(ACCESS_TOKEN_PREFIX)
    refresh = mint_secret(REFRESH_TOKEN_PREFIX)
    node = OAuthToken(
        uid=uuid4().hex,
        access_hash=hash_secret(access),
        refresh_hash=hash_secret(refresh),
        client_id=client_id,
        user_uid=user_uid,
        scope=scope,
        access_expires_at=now + ACCESS_TTL,
        refresh_expires_at=now + REFRESH_TTL,
    )
    await node.save()
    # The cleartext values leave the process exactly once, on this response.
    node._access_token = access  # noqa: SLF001 — transient, not persisted
    node._refresh_token = refresh  # noqa: SLF001
    return node


async def resolve_access_token(token: str) -> OAuthToken | None:
    """Middleware path: cleartext access token → live token node, or None."""
    if not (token or "").startswith(ACCESS_TOKEN_PREFIX):
        return None
    node = await OAuthToken.nodes.get_or_none(access_hash=hash_secret(token))
    if node is None or node.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    if node.access_expires_at and node.access_expires_at < now:
        return None
    if node.last_used_at is None or (now - node.last_used_at) > timedelta(minutes=5):
        node.last_used_at = now
        await node.save()
    return node
