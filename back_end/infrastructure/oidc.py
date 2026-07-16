"""Zitadel OIDC access-token verification (phase-1 multi-user auth).

TokenAuthMiddleware (app.py) calls verify_oidc_token() for any presented
bearer that isn't the shared secret or a `osrt_…` run token. Verification is
fully local: RS256 signature against the issuer's JWKS (cached in-process),
plus issuer / expiry / audience checks. On success the claims dict is stashed
in scope["state"]["oidc_claims"] and api.dependencies.get_current_user
resolves it to a User node (domains/users/services/oidc_user.py).

Zitadel specifics (see deployment/ZITADEL.md for the console walkthrough):
  - The SPA application must use Auth Token Type = JWT — Zitadel's default
    opaque access tokens can't be verified locally.
  - Roles arrive in `urn:zitadel:iam:org:project:roles` (or the
    project-id-scoped variant). OpenSweep's role names are used verbatim as
    Zitadel project roles: viewer / maintainer / admin.
  - ZITADEL_INTERNAL_URL lets containers fetch JWKS over the docker network;
    the request carries a Host header derived from ZITADEL_ISSUER so
    Zitadel's virtual-host instance resolution still matches.
"""

import asyncio
import time
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK

from config import settings
from logging_config import logger

_JWKS_PATH = "/oauth/v2/keys"
_USERINFO_PATH = "/oidc/v1/userinfo"
_REFETCH_MIN_INTERVAL = 30.0  # seconds between JWKS refetches on unknown kid

_keys: dict[str, PyJWK] = {}
_last_fetch: float = 0.0
_fetch_lock = asyncio.Lock()


def oidc_enabled() -> bool:
    return bool(settings.ZITADEL_ISSUER)


def _issuer() -> str:
    return settings.ZITADEL_ISSUER.rstrip("/")


def _base_url_and_host(path: str) -> tuple[str, dict[str, str]]:
    """URL + headers for a Zitadel endpoint: public issuer by default, the
    docker-network URL (with the issuer's Host header) when configured."""
    internal = settings.ZITADEL_INTERNAL_URL.rstrip("/")
    if not internal:
        return f"{_issuer()}{path}", {}
    # The issuer's full authority (host:port). Zitadel resolves the virtual
    # instance from Host AND derives the expected token issuer from it — a
    # port-less Host resolves the instance (discovery/JWKS work) but makes
    # userinfo reject access tokens minted for a ported issuer with
    # 401 "access token invalid".
    host = urlsplit(_issuer()).netloc
    return f"{internal}{path}", {"Host": host}


async def _refresh_jwks() -> None:
    global _keys, _last_fetch
    url, headers = _base_url_and_host(_JWKS_PATH)
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        body = res.json()
    keys: dict[str, PyJWK] = {}
    for jwk in body.get("keys", []):
        kid = jwk.get("kid", "")
        if not kid:
            # A key with no `kid` would collide on the "" slot and could be
            # resolved by a kid-less token — skip it (F8).
            logger.warning("OIDC: skipping JWKS key with no kid")
            continue
        try:
            keys[kid] = PyJWK(jwk)
        except Exception as exc:  # unsupported key type — skip, don't fail auth
            logger.warning(f"OIDC: skipping JWKS key {kid}: {exc}")
    _keys = keys
    _last_fetch = time.monotonic()
    logger.info(f"OIDC: JWKS refreshed ({len(keys)} keys) from {url}")


async def _signing_key(kid: str) -> PyJWK | None:
    if not kid:
        return None  # a token with no kid must never resolve a key (F8)
    if kid in _keys:
        return _keys[kid]
    async with _fetch_lock:
        if kid in _keys:  # refreshed while waiting on the lock
            return _keys[kid]
        if time.monotonic() - _last_fetch < _REFETCH_MIN_INTERVAL:
            return None  # unknown kid, recently refreshed — don't stampede
        try:
            await _refresh_jwks()
        except Exception as exc:
            logger.warning(f"OIDC: JWKS fetch failed: {exc}")
            return None
    return _keys.get(kid)


def _audience_ok(claims: dict) -> bool:
    """`aud` must include the SPA client id or the project id. If neither is
    configured we accept any audience from the trusted issuer (dev-friendly,
    logged at startup via deployment docs)."""
    accepted = {settings.ZITADEL_CLIENT_ID, settings.ZITADEL_PROJECT_ID} - {""}
    if not accepted:
        return True
    aud = claims.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]
    return bool(accepted.intersection(aud))


async def verify_oidc_token(token: str) -> dict | None:
    """Validate a Zitadel JWT access token. Returns claims, or None if the
    token is invalid / not a JWT / OIDC is disabled."""
    if not oidc_enabled():
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return None  # not a JWT (e.g. a mistyped shared token) — quiet
    key = await _signing_key(header.get("kid", ""))
    if key is None:
        logger.warning("OIDC: token rejected — no matching JWKS key")
        return None
    try:
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=["RS256"],
            issuer=_issuer(),
            options={"verify_aud": False},  # aud is a list of ids — checked below
        )
    except jwt.InvalidTokenError as exc:
        logger.info(f"OIDC: token rejected: {exc}")
        return None
    if not _audience_ok(claims):
        logger.info("OIDC: token rejected: audience mismatch")
        return None
    return claims


async def fetch_userinfo(access_token: str) -> dict:
    """Best-effort userinfo lookup (email/name aren't always minted into JWT
    access tokens). Failures return {} — never block auth on this."""
    url, headers = _base_url_and_host(_USERINFO_PATH)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as exc:
        logger.warning(f"OIDC: userinfo fetch failed: {exc}")
        return {}


# ── Claim mapping ────────────────────────────────────────────────────────────

_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
_ORG_CLAIM = "urn:zitadel:iam:user:resourceowner:id"
_ORG_NAME_CLAIM = "urn:zitadel:iam:user:resourceowner:name"


def zitadel_roles(claims: dict, project_id: str = "") -> set[str]:
    """Role keys from the roles claims.

    Tenancy (F5): when `project_id` is given, only OpenSweep's own project is
    trusted — the generic roles claim (Zitadel mints it for the token's primary
    audience) plus the claim scoped to that project id. This stops an `admin`
    role granted in some OTHER project on the same Zitadel issuer from being
    read as an OpenSweep grant. With no project configured (dev) the previous
    permissive behavior is kept for back-compat.
    """
    project_id = (project_id or "").strip()
    scoped_claim = (
        f"urn:zitadel:iam:org:project:{project_id}:roles" if project_id else ""
    )
    roles: set[str] = set()
    for key, value in claims.items():
        if not isinstance(value, dict):
            continue
        if key == _ROLES_CLAIM:
            roles.update(value.keys())
        elif project_id:
            if key == scoped_claim:
                roles.update(value.keys())
        elif key.startswith("urn:zitadel:iam:org:project:") and key.endswith(":roles"):
            roles.update(value.keys())
    return roles


def _opensweep_project_id() -> str:
    return (getattr(settings, "ZITADEL_PROJECT_ID", "") or "").strip()


def map_opensweep_role(claims: dict) -> str:
    """Highest OpenSweep role asserted in the token; least-privilege default.

    Pinned to OpenSweep's project (F5) so a foreign project's role names can't
    grant in-org capabilities on the pre-provisioned-org join seam."""
    granted = zitadel_roles(claims, _opensweep_project_id())
    for role in ("admin", "maintainer", "viewer"):
        if role in granted:
            return role
    return "viewer"


def is_platform_admin_claim(claims: dict) -> bool:
    """True iff the token asserts the OpenSweep instance-operator role.

    Pinned to OpenSweep's project id (F5): a bare `admin` role from an
    unrelated app on the same issuer must NOT confer platform-admin."""
    return "admin" in zitadel_roles(claims, _opensweep_project_id())


def primary_org_id(claims: dict) -> str:
    """The user's Zitadel organization id — the tenancy root."""
    return str(claims.get(_ORG_CLAIM, "") or "")


def primary_org_name(claims: dict) -> str:
    return str(claims.get(_ORG_NAME_CLAIM, "") or "")
