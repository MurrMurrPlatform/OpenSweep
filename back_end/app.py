"""OpenSweep FastAPI application."""

import hmac
import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from config import settings
from infrastructure.neomodel_config import configure_neomodel
from logging_config import configure_uvicorn_logging, logger

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


# ── ASGI Middleware ─────────────────────────────────────────────────────────


class NeomodelAsyncDriverMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] in ("http", "websocket"):
            try:
                from neomodel import adb
                from neomodel import config as neomodel_conf

                db_url = getattr(neomodel_conf, "DATABASE_URL", None)
                if db_url and not adb.driver:
                    await adb.set_connection(url=db_url)
            except Exception as exc:
                logger.debug(f"Neomodel async driver: {exc}")
        await self.app(scope, receive, send)


class TokenAuthMiddleware:
    """Shared-secret token auth (PLATFORM_V2_DESIGN.md §13) — v1.

    Disabled when settings.OPENSWEEP_AUTH_TOKEN is empty (local dev). When set,
    every HTTP and WebSocket request must present the token via:
      - `Authorization: Bearer <token>`, or
      - `X-OpenSweep-Auth: <token>`, or
      - `?auth_token=<token>` (WebSocket handshakes only — WebSocket clients
        cannot set headers).

    Exempt paths:
      - /health                       (container/LB healthchecks)
      - /api/v1/github/webhook        (HMAC-verified separately by the receiver)
      - /api/v1/github/app/setup      (GitHub redirects the installer's browser
                                       here after an App install — it carries no
                                       OpenSweep token; trust = the signed install
                                       state verified by the handler)
    /docs, /redoc and /openapi.json are gated like everything else.

    Per-run scoped tokens (infrastructure/run_tokens.py): agent executors
    never hold OPENSWEEP_AUTH_TOKEN — their mcp.json carries a `osrt_…` token
    derived from the run uid. Those are accepted ONLY on the platform-tool
    callback paths (/mcp/platform, /api/v1/platform-tools) and require the
    matching `X-OpenSweep-Run-Uid` header (mcp-remote already sends it), so a
    leaked run token opens nothing but the tool surface for its own run.

    Zitadel OIDC (infrastructure/oidc.py): when ZITADEL_ISSUER is set, a
    presented bearer that isn't the shared secret or a run token is verified
    as a Zitadel JWT access token (JWKS signature + issuer/expiry/audience).
    Verified claims land in scope["state"]["oidc_claims"] for
    get_current_user. Both mechanisms stay active side by side: browsers
    log in via Zitadel, internal/executor clients keep the shared token.
    Auth is enforced whenever either OPENSWEEP_AUTH_TOKEN or ZITADEL_ISSUER is
    configured.

    Failure: HTTP → 401 JSON; WebSocket → accept-then-close with code 4401
    (a pre-accept denial would surface as an opaque handshake 403).
    """

    EXEMPT_PATHS = frozenset(
        {
            "/health",
            "/api/v1/github/webhook",
            # GitHub redirects the installer's browser here after an App
            # install — no OpenSweep token; trust = the signed install state
            # (org-bound) verified by the handler.
            "/api/v1/github/app/setup",
            # Slack redirects the installing admin's browser here after an
            # OAuth v2 install — trust = the signed install state.
            "/api/v1/slack/oauth/callback",
            # Slack servers POST here — trust = the v0 request signature
            # (SLACK_SIGNING_SECRET) verified by the handlers.
            "/api/v1/slack/events",
            "/api/v1/slack/commands",
            # OAuth gateway for `opensweep connect` (api/oauth_mcp.py) — the
            # front door for MCP clients that don't hold credentials yet.
            # /oauth/token mints nothing without a PKCE-verified code from an
            # authenticated consent; /oauth/authorize only redirects.
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
            "/oauth/register",
            "/oauth/authorize",
            "/oauth/token",
        }
    )

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _presented_token(scope: Scope) -> str:
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        opensweep = headers.get(b"x-opensweep-auth", b"").decode("latin-1").strip()
        if opensweep:
            return opensweep
        # Query-param fallback for header-less clients: WebSocket handshakes
        # only. Not accepted on the general REST surface — tokens don't
        # belong in URLs/access logs.
        if scope["type"] == "websocket":
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            values = qs.get("auth_token") or []
            if values:
                return values[0].strip()
        return ""

    @staticmethod
    def _run_token_paths() -> tuple[str, ...]:
        # Every path that backs a mounted platform tool: fastapi-mcp executes
        # tool calls by re-entering the ASGI app at the tool's REST route, so
        # the osrt_ token must be valid there too — /api/v1/platform-read backs
        # the opensweep_platform_read_* (look-before-write) tools.
        mount = getattr(settings, "MCP_PLATFORM_TOOL_MOUNT_PATH", "/mcp/platform") or "/mcp/platform"
        return (mount, "/api/v1/platform-tools", "/api/v1/platform-read")

    # Paths an `osmcp_` OAuth access token may reach: the external MCP mount
    # plus the REST endpoints backing its mounted operations. Everything else
    # (admin, settings, LLM providers, …) stays closed to connect tokens.
    MCP_TOKEN_PATH_PREFIXES = (
        "/api/v1/repositories",
        "/api/v1/findings",
        "/api/v1/freshness",
        "/api/v1/agents",
        "/api/v1/scheduled-agents",
        "/api/v1/docs",
        "/api/v1/memories",
        "/api/v1/run-policies",
        "/api/v1/audit",
        "/api/v1/tickets",
        "/api/v1/threads",
        "/api/v1/delivery",
        "/api/v1/comments",
    )

    @classmethod
    async def _mcp_token_state(cls, scope: Scope, presented: str) -> dict | None:
        """OAuth connect token (`osmcp_`): resolve to a user, enforce the path
        allowlist and the read/write scope split. Returns the state additions
        or None when the token doesn't authorize this request."""
        from domains.oauth_mcp.services.oauth_service import (
            ACCESS_TOKEN_PREFIX,
            resolve_access_token,
            scope_allows_write,
        )

        if not presented.startswith(ACCESS_TOKEN_PREFIX):
            return None
        path = scope.get("path", "")
        mount = (getattr(settings, "MCP_MOUNT_PATH", "/mcp") or "/mcp").rstrip("/")
        on_mount = path == mount or path.startswith(mount + "/")
        on_api = any(
            path == p or path.startswith(p + "/") for p in cls.MCP_TOKEN_PATH_PREFIXES
        )
        if not on_mount and not on_api:
            return None
        token = await resolve_access_token(presented)
        if token is None:
            return None
        method = (scope.get("method") or "GET").upper()
        if on_api and method not in {"GET", "HEAD", "OPTIONS"}:
            # mcp:read still allows discussion (comment_create); every other
            # write needs the mcp:write consent.
            comment_write = path.rstrip("/") == "/api/v1/comments" and method == "POST"
            if not comment_write and not scope_allows_write(token.scope):
                return None
        return {"mcp_token_user_uid": token.user_uid, "mcp_token_scope": token.scope}

    @classmethod
    def _run_token_allowed(cls, scope: Scope, presented: str) -> bool:
        """Scoped `osrt_…` run token: valid only on the platform-tool callback
        paths, and only together with its own X-OpenSweep-Run-Uid header (the
        token is recomputed from the header — both must be present + match)."""
        from infrastructure.run_tokens import RUN_TOKEN_PREFIX, verify_run_token

        if not presented.startswith(RUN_TOKEN_PREFIX):
            return False
        path = scope.get("path", "")
        if not any(path == p or path.startswith(p + "/") for p in cls._run_token_paths()):
            return False
        headers = dict(scope.get("headers", []))
        run_uid = headers.get(b"x-opensweep-run-uid", b"").decode("latin-1").strip()
        return bool(run_uid) and verify_run_token(presented, run_uid)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        expected = settings.OPENSWEEP_AUTH_TOKEN
        oidc_on = bool(settings.ZITADEL_ISSUER)
        if not expected and not oidc_on:  # auth disabled — local dev
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] == "http" and path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        presented = self._presented_token(scope)
        if (
            presented
            and expected
            and hmac.compare_digest(presented.encode(), expected.encode())
        ):
            await self.app(scope, receive, send)
            return
        if presented:
            mcp_state = await self._mcp_token_state(scope, presented)
            if mcp_state is not None:
                scope.setdefault("state", {}).update(mcp_state)
                await self.app(scope, receive, send)
                return
        if presented and self._run_token_allowed(scope, presented):
            # Record the run identity: platform-tool routes pin run-token
            # callers to their own run's repository (api/platform_scope.py).
            headers = dict(scope.get("headers", []))
            run_uid = headers.get(b"x-opensweep-run-uid", b"").decode("latin-1").strip()
            scope.setdefault("state", {})["run_token_uid"] = run_uid
            await self.app(scope, receive, send)
            return
        if presented and oidc_on:
            from infrastructure.oidc import verify_oidc_token

            claims = await verify_oidc_token(presented)
            if claims:
                state = scope.setdefault("state", {})
                state["oidc_claims"] = claims
                state["oidc_access_token"] = presented
                await self.app(scope, receive, send)
                return

        if scope["type"] == "websocket":
            # Consume the connect event, then accept + close so the client
            # sees a clean 4401 close code instead of a handshake failure.
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return

        body = json.dumps({"detail": "unauthorized"}).encode()
        # MCP authorization discovery: a 401 from the external mount points
        # clients at the protected-resource metadata (api/oauth_mcp.py), which
        # kicks off the OAuth flow for `opensweep connect`.
        challenge = b"Bearer"
        mount = (getattr(settings, "MCP_MOUNT_PATH", "/mcp") or "/mcp").rstrip("/")
        if path == mount or path.startswith(mount + "/"):
            resource_md = (
                f"{(settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL or '').rstrip('/')}"
                "/.well-known/oauth-protected-resource"
            )
            challenge = f'Bearer resource_metadata="{resource_md}"'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", challenge),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        incoming = headers.get(b"x-request-id", b"").decode() or None
        rid = incoming or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(rid)

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": hdrs}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_ctx.reset(token)


# ── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production guards — deliberately OUTSIDE any try/except: booting a
    # production deployment with no auth or a default database password must
    # go unhealthy instead of serving traffic (same rationale as the schema
    # migration block below).
    from infrastructure.production_guards import enforce_production_guards

    enforce_production_guards(settings)

    logger.info("Starting OpenSweep...")
    configure_uvicorn_logging()

    try:
        configure_neomodel()
        logger.info("Neomodel configured")
    except Exception as exc:
        logger.warning(f"Neomodel configuration skipped: {exc}")

    neo4j_ready = False
    try:
        from neomodel import adb
        from neomodel import config as neomodel_conf

        db_url = getattr(neomodel_conf, "DATABASE_URL", None)
        if db_url:
            await adb.set_connection(url=db_url)
            await adb.cypher_query("RETURN 1")
            neo4j_ready = True
            logger.info("Neo4j connection validated")

        from infrastructure.neomodel_bootstrap import create_constraints

        await create_constraints()
    except Exception as exc:
        logger.warning(f"Neo4j bootstrap skipped: {exc}")

    if neo4j_ready:
        # Schema migrations — deliberately OUTSIDE a try/except: a migration
        # failure (or an irreversible version skew after a deployment
        # rollback) must abort the boot so the deploy goes unhealthy instead
        # of serving a half-migrated graph. Each migration applies
        # transactionally; auto-rollback covers the Coolify rollback case.
        from config import settings as _settings
        from infrastructure.migration_runner import migrate

        plan = await migrate(
            auto_rollback=_settings.OPENSWEEP_MIGRATIONS_AUTO_ROLLBACK,
            lock_timeout_seconds=_settings.OPENSWEEP_MIGRATIONS_LOCK_TIMEOUT_SECONDS,
        )
        if not plan.noop:
            logger.info(
                f"Schema migrations: reverted {[r.version for r in plan.to_revert]}, "
                f"applied {[m.version for m in plan.to_apply]}"
            )

    try:
        # Idempotent org-membership provisioning: stamps pre-tenancy users,
        # repairs owner-less orgs, backfills GitHub installation links. Safe
        # (and required) on both first and repeat deployments.
        from domains.organizations.services.provisioning import migrate_tenancy

        await migrate_tenancy()
    except Exception as exc:
        logger.warning(f"Tenancy migration skipped: {exc}")

    try:
        # Secrets at rest: when OPENSWEEP_SECRETS_KEY is configured, one
        # idempotent pass seals any plaintext LLMProvider credentials (and
        # re-seals rows still under a fallback key). Best-effort — a failure
        # here degrades to plaintext-at-rest rather than blocking the boot.
        from infrastructure import secretbox

        if secretbox.configured():
            from domains.llm_providers.services.credentials import (
                encrypt_plaintext_provider_secrets,
            )

            sealed = await encrypt_plaintext_provider_secrets()
            if sealed:
                logger.info(f"Sealed {sealed} plaintext LLM provider credential(s)")
    except Exception as exc:
        logger.warning(f"Provider credential sealing skipped: {exc}")

    try:
        # Dispatch tasks are asyncio tasks inside THIS process — any run still
        # queued/running from a previous backend life died with it. Fail those
        # rows now so the UI and the 409 dispatch guard stop trusting them.
        from domains.runs.services.run_reconciliation import (
            reconcile_orphaned_runs,
        )
        from infrastructure.process_role import BACKEND

        orphaned = await reconcile_orphaned_runs(role=BACKEND)
        if orphaned:
            logger.info(f"Failed {orphaned} orphaned run(s) from a previous backend process")
    except Exception as exc:
        logger.warning(f"Orphaned-run sweep skipped: {exc}")

    try:
        # Unified platform seeding — one ordered, idempotent, lease-locked pass
        # (system RunPolicy, prompt library, per-repo docs + Investigations).
        # SYNC rolls shipped-default improvements forward onto rows the user
        # hasn't edited; hand-tuned rows are preserved. The "dev" group (local
        # User, baseline LLMProviders) is deliberately NOT run at startup — a
        # boot must never touch provider credentials. The whole pass is
        # best-effort (each seeder records its own error) so a transient seed
        # failure degrades rather than blocking the boot.
        from infrastructure.seeding import SeedMode, run_seeders, summarize

        results = await run_seeders(SeedMode.SYNC)
        logger.info(f"Platform seeding: {summarize(results)}")
    except Exception as exc:
        logger.warning(f"Platform seeding skipped: {exc}")

    logger.info("OpenSweep started")
    yield
    logger.info("Shutting down OpenSweep...")


# ── Routers ─────────────────────────────────────────────────────────────────


def _include_routers(application: FastAPI):
    """Mount the v1 router surface.

    Primitives (KNOWLEDGE_V3):
      - repositories, docs       — where (the doc tree is the concept layer)
      - agents + scheduled-agents — what to look for, and when
      - freshness (checked)      — what was checked, when, at which revision
      - findings                 — what was found
      - docs + memories          — what's been learned
      - run_policies             — bounds every Run

    Internal machinery (executions/sandboxes/environments) is kept only for
    disposable tracking-only agent inspection.
    """
    routers = [
        "api.v1.meta",
        "api.v1.organizations",
        "api.v1.repositories",
        "api.v1.platform_config",
        "api.v1.sweep",
        "api.v1.findings",
        "api.v1.analysis",
        "api.v1.freshness",
        "api.v1.workflow",
        "api.v1.artifacts",
        "api.v1.agents",
        "api.v1.scheduled_agents",
        "api.v1.runs",
        "api.v1.docs",
        "api.v1.memories",
        "api.v1.run_policies",
        "api.v1.platform_tools",
        "api.v1.platform_read",
        "api.v1.audit",
        # Delivery — PR convergence ledger (PLATFORM_V2_DESIGN.md)
        "api.v1.delivery",
        "api.v1.github_webhooks",
        "api.v1.github_app",
        "api.v1.git_connections",
        "api.v1.platform_tools_delivery",
        # Tickets — Phase 2 work management (Gate 1 lives here)
        "api.v1.tickets",
        "api.v1.ticket_groups",
        "api.v1.platform_tools_tickets",
        # Threads — unified dev flow: one conversation per ticket
        "api.v1.threads",
        # `opensweep connect` OAuth gateway: public front door + consent
        "api.oauth_mcp",
        "api.v1.oauth_mcp",
        # Comments — discussion threads on any data item (@opensweep summons a run)
        "api.v1.comments",
        "api.v1.mentions",
        "api.v1.platform_tools_comments",
        # News board + interests — the news scout's inbox (human-only conversion)
        "api.v1.news",
        "api.v1.interests",
        # Slack — per-org workspace connection, notification rules, inbound bot
        "api.v1.slack",
        # Notifications — the in-app inbox / attention centre (audit-derived)
        "api.v1.notifications",
        # Internal machinery (Admin)
        "api.v1.llm_providers",
        "api.v1.sandboxes",
    ]
    for path in routers:
        try:
            module = __import__(path, fromlist=["router"])
            if hasattr(module, "router"):
                application.include_router(module.router)
        except ImportError as exc:
            logger.warning(f"Router skipped ({path}): {exc}")


# ── App factory ─────────────────────────────────────────────────────────────


app = FastAPI(
    title="OpenSweep — Cost-Aware Repo Intelligence Platform",
    description=(
        "Repo intelligence + AI dev-workflow platform: Docs, Memories, "
        "Checked stamps, Investigations, Runs, Findings, and the delivery "
        "loop (Tickets, PRs, review verdicts, convergence)."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# Added first → innermost user middleware: CORS (added after, therefore
# outer) answers browser preflights before auth sees them.
app.add_middleware(TokenAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.ALLOWED_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)
app.add_middleware(NeomodelAsyncDriverMiddleware)
app.add_middleware(RequestIDMiddleware)

_include_routers(app)


@app.get("/health")
async def health() -> dict:
    neo4j_ok = False
    try:
        from neomodel import adb
        await adb.cypher_query("RETURN 1")
        neo4j_ok = True
    except Exception:
        pass
    return {
        "status": "healthy" if neo4j_ok else "degraded",
        "services": {"neo4j": "ok" if neo4j_ok else "unavailable"},
    }


# Mount MCP last (after routes + operation_ids are set on each route)
from mcp_app import mount_mcp  # noqa: E402

mount_mcp(app)


def _install_cloud_overlay(application: FastAPI) -> None:
    """Open-core seam: OpenSweep Cloud ships an additive `cloud_overlay`
    package (cloud repo only) whose `install(app)` registers cloud-only
    behavior — entitlement hooks, production guards, extra routes. The
    public product has no such package, so this is a no-op here; never put
    cloud logic in shared files (CLAUDE.md open-core rules).

    Gated behind OPENSWEEP_CLOUD_OVERLAY=1: an unconditional import of a
    generically-named top-level module is a dependency-confusion seam (any
    `cloud_overlay` landing on sys.path would execute at boot). The cloud
    deployment sets the flag explicitly."""
    import importlib
    import os

    if os.environ.get("OPENSWEEP_CLOUD_OVERLAY", "").strip().lower() not in {"1", "true"}:
        return
    try:
        module = importlib.import_module("cloud_overlay")
    except ModuleNotFoundError:
        logger.warning(
            "OPENSWEEP_CLOUD_OVERLAY set but no cloud_overlay package found",
            extra={"tag": "startup"},
        )
        return
    install = getattr(module, "install", None)
    if callable(install):
        install(application)
        logger.info("cloud overlay installed", extra={"tag": "startup"})


_install_cloud_overlay(app)
