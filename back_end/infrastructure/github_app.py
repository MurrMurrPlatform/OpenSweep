"""GitHub App auth — App JWT + short-lived installation tokens (§7).

When a GitHub App is connected (infrastructure/github_app_store.py), all
GitHub API calls and git clones/pushes for repos that carry a
`github_installation_id` use installation tokens minted here from the App
JWT. Repos without an installation (or when no App is connected) keep using
the PAT (`settings.GITHUB_TOKEN`) — nothing changes for PAT-only deployments.

Token precedence, everywhere a GitHub credential is needed:
  1. installation token   — App connected AND repo.github_installation_id set
  2. settings.GITHUB_TOKEN — the PAT fallback (also the migration path)
"""

from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import redis.exceptions

from config import settings
from infrastructure import redis_client
from infrastructure.github_app_store import GitHubAppConfig, get_github_app
from logging_config import logger

_GITHUB_API = "https://api.github.com"

# Installation tokens live ~60 min; re-mint when within this margin of expiry.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def make_app_jwt(app_id: str, pem: str, *, now: int | None = None) -> str:
    """RS256 App JWT per GitHub docs: iat 60s in the past (clock drift),
    exp 9 min out (GitHub caps at 10)."""
    ts = int(now if now is not None else time.time())
    payload = {"iat": ts - 60, "exp": ts + 540, "iss": str(app_id)}
    return jwt.encode(payload, pem, algorithm="RS256")


# ── Installation token cache ─────────────────────────────────────────────────


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


_token_cache: dict[int, _CachedToken] = {}

# asyncio.Lock binds to the loop it is first awaited on; Celery tasks run in
# fresh asyncio.run() loops, so keep one lock per loop (strong loop ref so an
# id() is never reused while cached — mirrors github_client._clients_by_loop).
_locks_by_loop: dict[int, tuple[Any, asyncio.Lock]] = {}


def _mint_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _locks_by_loop.get(id(loop))
    if entry is not None and entry[0] is loop:
        return entry[1]
    for key in [k for k, (cached, _) in _locks_by_loop.items() if cached.is_closed()]:
        _locks_by_loop.pop(key, None)
    lock = asyncio.Lock()
    _locks_by_loop[id(loop)] = (loop, lock)
    return lock


def _cache_fresh(entry: _CachedToken | None, *, now: datetime) -> bool:
    return entry is not None and now < entry.expires_at - TOKEN_REFRESH_MARGIN


# ── Redis L2 (shared across replicas; L1 = _token_cache stays authoritative
#    within a process) ─────────────────────────────────────────────────────────

_REDIS_TOKEN_PREFIX = "opensweep:ghapp:inst_token:"
_REDIS_MINT_LOCK_PREFIX = "opensweep:ghapp:mint_lock:"
_MINT_LOCK_TTL_SECONDS = 30
_MINT_POLL_INTERVAL = 0.25
_MINT_POLL_TIMEOUT = 10.0

_warned_redis_degraded = False


def _warn_redis_degraded(exc: Exception) -> None:
    global _warned_redis_degraded
    if not _warned_redis_degraded:
        _warned_redis_degraded = True
        logger.warning(
            "installation token cache: Redis unreachable — degrading to the "
            f"in-process cache only (per-replica mints): {exc}",
            extra={"tag": "github"},
        )


def _encode_cached_token(entry: _CachedToken) -> str:
    """JSON payload for Redis; sealed at rest when a secrets key is set."""
    payload = json.dumps({"token": entry.token, "expires_at": entry.expires_at.isoformat()})
    from infrastructure import secretbox

    if secretbox.configured():
        return secretbox.seal(payload)
    return payload


def _decode_cached_token(raw: str) -> _CachedToken | None:
    """None on any malformed/undecryptable value — treated as a cache miss."""
    from infrastructure import secretbox

    try:
        data = json.loads(secretbox.unseal(raw))
        token = str(data.get("token") or "")
        expires_at = datetime.fromisoformat(str(data.get("expires_at") or ""))
        if not token:
            return None
        return _CachedToken(token=token, expires_at=expires_at)
    except (secretbox.SecretBoxError, ValueError, TypeError, AttributeError):
        return None


async def _redis_read_token(installation_id: int) -> _CachedToken | None:
    raw = await redis_client.get_async_redis().get(f"{_REDIS_TOKEN_PREFIX}{installation_id}")
    if not raw:
        return None
    return _decode_cached_token(raw)


async def _redis_write_token(installation_id: int, entry: _CachedToken) -> None:
    ttl = max(1, int((entry.expires_at - datetime.now(UTC)).total_seconds()))
    await redis_client.get_async_redis().set(
        f"{_REDIS_TOKEN_PREFIX}{installation_id}", _encode_cached_token(entry), ex=ttl
    )


def _parse_expires_at(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        # Defensive: an unparseable expiry means "treat as short-lived".
        return datetime.now(UTC) + timedelta(minutes=10)


async def _request_installation_token(app: GitHubAppConfig, installation_id: int) -> dict:
    """POST /app/installations/{id}/access_tokens with the App JWT.

    Retried through the shared GitHub policy (`send_with_retry`): minting is
    the FIRST call in every delivery operation on an App-connected repo, so a
    transient 5xx here fails the operation before any of the retry-protected
    list/publish calls are reached. `retry_unsafe=True` is safe despite the
    POST: minting is idempotent — a duplicate mint just returns another
    short-lived token, it does not create anything the user sees.

    Module-level seam so tests can monkeypatch it without real HTTP.
    """
    from infrastructure.github_client import send_with_retry

    app_jwt = make_app_jwt(app.app_id, app.pem)
    async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=15) as client:
        r = await send_with_retry(
            client,
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            retry_unsafe=True,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        return r.json()


async def _mint_token(installation_id: int) -> _CachedToken:
    """One real mint via the GitHub API. Raises on any mint failure."""
    app = get_github_app()
    if app is None:
        raise RuntimeError("no GitHub App connected — cannot mint an installation token")
    body = await _request_installation_token(app, installation_id)
    token = str(body.get("token") or "")
    if not token:
        raise RuntimeError(f"GitHub returned no token for installation {installation_id}")
    return _CachedToken(
        token=token, expires_at=_parse_expires_at(body.get("expires_at") or "")
    )


async def _mint_under_local_lock(installation_id: int) -> str:
    """Pre-Redis behavior: per-process single flight via the asyncio lock."""
    async with _mint_lock():
        # Re-check under the lock — a concurrent caller may have minted.
        entry = _token_cache.get(installation_id)
        if _cache_fresh(entry, now=datetime.now(UTC)):
            return entry.token  # type: ignore[union-attr]
        entry = await _mint_token(installation_id)
        _token_cache[installation_id] = entry
        return entry.token


async def _get_token_via_redis(installation_id: int) -> str:
    """Redis L2 read + cross-replica single-flight mint. RedisError/OSError
    propagate to the caller (which degrades to local behavior); mint failures
    propagate untouched."""
    client = redis_client.get_async_redis()

    entry = await _redis_read_token(installation_id)
    if _cache_fresh(entry, now=datetime.now(UTC)):
        _token_cache[installation_id] = entry  # type: ignore[assignment]
        return entry.token  # type: ignore[union-attr]

    lock_key = f"{_REDIS_MINT_LOCK_PREFIX}{installation_id}"
    lock_token = pysecrets.token_hex(16)
    acquired = await client.set(lock_key, lock_token, nx=True, ex=_MINT_LOCK_TTL_SECONDS)
    if acquired:
        try:
            # Double-check: another replica may have minted between the read
            # above and our lock acquisition.
            entry = await _redis_read_token(installation_id)
            if _cache_fresh(entry, now=datetime.now(UTC)):
                _token_cache[installation_id] = entry  # type: ignore[assignment]
                return entry.token  # type: ignore[union-attr]
            entry = await _mint_token(installation_id)
            _token_cache[installation_id] = entry
            try:
                await _redis_write_token(installation_id, entry)
            except (redis.exceptions.RedisError, OSError) as exc:
                _warn_redis_degraded(exc)  # token already minted — L1 has it
            return entry.token
        finally:
            # Best-effort compare-and-delete: only release a lock we still own.
            try:
                if await client.get(lock_key) == lock_token:
                    await client.delete(lock_key)
            except (redis.exceptions.RedisError, OSError):
                pass

    # Another holder is minting — poll the cache key until it appears.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MINT_POLL_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(_MINT_POLL_INTERVAL)
        entry = await _redis_read_token(installation_id)
        if _cache_fresh(entry, now=datetime.now(UTC)):
            _token_cache[installation_id] = entry  # type: ignore[assignment]
            return entry.token  # type: ignore[union-attr]
        # Lock released without a token written → the holder's mint failed;
        # don't sleep out the full timeout, mint ourselves now.
        if not await client.get(lock_key):
            break
    # Holder died, failed, or is wedged — mint ourselves under the local lock.
    return await _mint_under_local_lock(installation_id)


async def get_installation_token(installation_id: int) -> str:
    """Short-lived installation token, cached until 5 min before expiry.

    L1: in-process _token_cache. L2: Redis (shared across replicas, sealed at
    rest when OPENSWEEP_SECRETS_KEY is set) with a cross-replica single-flight
    mint lock. Any Redis trouble degrades to the pre-Redis local behavior."""
    installation_id = int(installation_id)
    entry = _token_cache.get(installation_id)
    if _cache_fresh(entry, now=datetime.now(UTC)):
        return entry.token  # type: ignore[union-attr]

    try:
        return await _get_token_via_redis(installation_id)
    except (redis.exceptions.RedisError, OSError) as exc:
        _warn_redis_degraded(exc)
        return await _mint_under_local_lock(installation_id)


async def clear_token_cache() -> None:
    """Testing/disconnect hook — clears L1 and best-effort sweeps the Redis
    L2 (token + mint-lock keys). Redis errors are swallowed: disconnect must
    always succeed."""
    _token_cache.clear()
    try:
        client = redis_client.get_async_redis()
        keys: list = []
        async for key in client.scan_iter(match=f"{_REDIS_TOKEN_PREFIX}*"):
            keys.append(key)
        async for key in client.scan_iter(match=f"{_REDIS_MINT_LOCK_PREFIX}*"):
            keys.append(key)
        if keys:
            await client.delete(*keys)
    except (redis.exceptions.RedisError, OSError):
        pass


# ── App-level reads (JWT-authenticated) ──────────────────────────────────────


async def list_installations() -> list[dict]:
    """GET /app/installations with the App JWT. Raises when no App connected."""
    from infrastructure.github_client import send_with_retry

    app = get_github_app()
    if app is None:
        raise RuntimeError("no GitHub App connected")
    app_jwt = make_app_jwt(app.app_id, app.pem)
    async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=15) as client:
        r = await send_with_retry(
            client,
            "GET",
            "/app/installations?per_page=100",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else []


# Defensive upper bound when listing an installation's repositories — repo
# discovery only needs enough for a selection UI, not an unbounded mirror.
MAX_INSTALLATION_REPOS = 300


async def list_installation_repositories(installation_id: int) -> list[dict]:
    """All repositories one installation grants access to — raw GitHub repo
    dicts from GET /installation/repositories (installation token), paginated
    (per_page=100, Link rel="next" loop), capped at MAX_INSTALLATION_REPOS.

    Raises on failure — callers decide how to surface per-installation errors.
    """
    token = await get_installation_token(installation_id)
    repos: list[dict] = []
    async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=15) as client:
        url: str | None = "/installation/repositories?per_page=100"
        while url and len(repos) < MAX_INSTALLATION_REPOS:
            r = await client.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            r.raise_for_status()
            body = r.json()
            page = body.get("repositories") if isinstance(body, dict) else None
            repos.extend(p for p in (page or []) if isinstance(p, dict))
            url = (r.links.get("next") or {}).get("url")
    return repos[:MAX_INSTALLATION_REPOS]


async def count_installation_repos(installation_id: int) -> int | None:
    """total_count from GET /installation/repositories (installation token).
    None on any failure — status callers tolerate missing counts."""
    try:
        token = await get_installation_token(installation_id)
        async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=15) as client:
            r = await client.get(
                "/installation/repositories?per_page=1",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            r.raise_for_status()
            return int(r.json().get("total_count") or 0)
    except Exception as exc:
        logger.warning(
            f"repo count for installation {installation_id} failed: {exc}", extra={"tag": "github"}
        )
        return None


# ── Repo-scoped credential selection ─────────────────────────────────────────


def repo_installation_id(repo: Any) -> int | None:
    """`github_installation_id` off a Repository node/DTO — pure, testable."""
    raw = getattr(repo, "github_installation_id", None)
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value or None


def repo_connection_uid(repo: Any) -> str:
    """`git_connection_uid` off a Repository node/DTO — the PAT connection
    the repo was registered through ("" when none)."""
    return str(getattr(repo, "git_connection_uid", None) or "")


async def get_connection_pat(connection_uid: str) -> str:
    """The sealed PAT of one GitConnection(kind="pat") — "" when the
    connection is gone or undecryptable (callers fall through to the env PAT)."""
    if not connection_uid:
        return ""
    from domains.organizations.models import GitConnection
    from domains.organizations.services.git_connections import connection_token

    conn = await GitConnection.nodes.get_or_none(uid=connection_uid, kind="pat")
    if conn is None:
        logger.warning(
            f"git connection {connection_uid} not found — a repo still points "
            "at a deleted connection; falling back to the env PAT",
            extra={"tag": "github"},
        )
        return ""
    return connection_token(conn)


def uses_app_auth(repo: Any, *, app_connected: bool) -> bool:
    """Pure selection rule: installation token iff an App is connected AND
    the repo carries an installation id."""
    return app_connected and repo_installation_id(repo) is not None


async def get_repo_git_token(repo: Any) -> str:
    """The git/API credential for one repo. Precedence: installation token →
    the repo's PAT-connection token → the global env PAT. Empty string when
    nothing is configured.

    A mint failure falls back to the next tier when one exists (migration
    safety); with nothing to fall back to the error propagates so callers
    surface a real cause.
    """
    installation_id = repo_installation_id(repo)
    if installation_id is not None and get_github_app() is not None:
        try:
            return await get_installation_token(installation_id)
        except Exception as exc:
            fallback = await get_connection_pat(repo_connection_uid(repo)) or settings.GITHUB_TOKEN
            if fallback:
                logger.warning(
                    f"installation token mint failed ({exc}) — falling back to PAT",
                    extra={"tag": "github"},
                )
                return fallback
            raise
    if installation_id is not None:
        # The repo was registered through an App installation but the App is
        # not loadable (missing/unparseable GITHUB_APP_ID or private key), so
        # this silently pushes as whichever human owns the PAT instead. That
        # substitution used to leave no trace at all, which made a later 403
        # impossible to attribute.
        logger.warning(
            f"repo is registered to installation {installation_id} but no GitHub App is "
            "configured — using the PAT credential instead",
            extra={"tag": "github"},
        )
    connection_token = await get_connection_pat(repo_connection_uid(repo))
    if connection_token:
        return connection_token
    return settings.GITHUB_TOKEN


async def describe_repo_credential(repo: Any) -> str:
    """Which credential `get_repo_git_token` will pick, in words.

    Mirrors that function's precedence exactly — a message naming the wrong
    credential is worse than no message. Used by the write preflight so a
    permission failure points at the thing to actually go and fix.
    """
    installation_id = repo_installation_id(repo)
    if installation_id is not None and get_github_app() is not None:
        return f"the GitHub App installation ({installation_id})"

    connection_uid = repo_connection_uid(repo)
    if connection_uid:
        from domains.organizations.models import GitConnection
        from domains.organizations.services.git_connections import connection_token

        conn = await GitConnection.nodes.get_or_none(uid=connection_uid, kind="pat")
        if conn is None:
            # `delete_pat_connection` leaves this pointer dangling on purpose,
            # expecting GITHUB_TOKEN to take over. When that is unset — the
            # normal self-hosted case — rotating a PAT instead unhooks every
            # repo registered through the old connection, and the only symptom
            # used to be an empty-credential push failure after a whole run.
            return (
                f"the git connection this repository was registered through "
                f"({connection_uid[:8]}) no longer exists — it was probably replaced "
                "by a newer connection, which repositories are not re-pointed to"
            )
        if connection_token(conn):
            name = (conn.display_name or "").strip()
            return (
                f"the personal access token connected as {name!r}"
                if name
                else f"the personal access token connection {connection_uid[:8]}"
            )

    if settings.GITHUB_TOKEN:
        return "the GITHUB_TOKEN environment credential"
    return "no configured credential"


class InstallationTokenSource:
    """TokenSource (infrastructure/github_client.py) backed by one installation."""

    def __init__(self, installation_id: int) -> None:
        self.installation_id = int(installation_id)

    async def get_token(self) -> str:
        return await get_installation_token(self.installation_id)


class ConnectionTokenSource:
    """TokenSource backed by one GitConnection(kind="pat") — resolved per
    request (one indexed node read + unseal) so a rotated/removed connection
    takes effect without invalidating cached clients. Falls back to the env
    PAT when the connection is gone."""

    def __init__(self, connection_uid: str) -> None:
        self.connection_uid = str(connection_uid)

    async def get_token(self) -> str:
        return await get_connection_pat(self.connection_uid) or settings.GITHUB_TOKEN


def get_client_for_repo(repo: Any):
    """GitHubClient bound to the repo's credential: installation-token client
    when the App covers this repo, else the repo's PAT-connection client,
    else the default (env PAT) client."""
    from infrastructure.github_client import (
        get_connection_client,
        get_default_client,
        get_installation_client,
    )

    installation_id = repo_installation_id(repo)
    if installation_id is not None and get_github_app() is not None:
        return get_installation_client(installation_id)
    connection_uid = repo_connection_uid(repo)
    if connection_uid:
        return get_connection_client(connection_uid)
    return get_default_client()
