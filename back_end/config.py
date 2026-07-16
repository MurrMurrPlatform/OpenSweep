"""Pydantic settings for OpenSweep."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Environment
    ENVIRONMENT: str = "local"
    LOG_LEVEL: str = "INFO"
    LOG_SERVICE_NAME: str = "opensweep_backend"

    # Neo4j
    NEO4J_HOST: str = "opensweep_neo4j"
    NEO4J_PORT: int = 7687
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "koalapassword"  # data-coupled legacy default: existing volumes were initialized with it

    # Redis
    REDIS_HOST: str = "opensweep_redis"
    REDIS_PORT: int = 6379
    REDIS_ACCESS_KEY: str = ""

    # Local user (no auth in v1)
    OPENSWEEP_LOCAL_USER_EMAIL: str = "local@opensweep.dev"
    OPENSWEEP_LOCAL_USER_DISPLAY_NAME: str = "Local User"

    # Shared-secret token auth (PLATFORM_V2_DESIGN.md §13) — v1 internet
    # hardening. Empty (default) = auth disabled, local dev unchanged.
    # When set, every HTTP/WS request must carry the token via
    # `Authorization: Bearer <token>`, `X-OpenSweep-Auth: <token>`, or (WS
    # only) `?auth_token=<token>`. Exempt: /health and the HMAC-verified
    # GitHub webhook. /docs + /openapi.json are gated.
    OPENSWEEP_AUTH_TOKEN: str = ""

    # Dedicated HMAC key for GitHub App state nonces; falls back to
    # OPENSWEEP_RUN_TOKEN_SECRET/OPENSWEEP_AUTH_TOKEN (wired in a later change).
    OPENSWEEP_STATE_SIGNING_SECRET: str = ""
    # Encrypts secrets at rest (infrastructure/secretbox.py, enc:v1:).
    # Any string >= 16 chars. Losing it makes sealed secrets UNRECOVERABLE.
    OPENSWEEP_SECRETS_KEY: str = ""
    # Comma-separated previous keys kept decryptable during rotation.
    OPENSWEEP_SECRETS_KEY_FALLBACKS: str = ""

    # ── Zitadel OIDC (multi-user auth) ──────────────────────────────────────
    # Empty ZITADEL_ISSUER = OIDC disabled. When set, TokenAuthMiddleware also
    # accepts Zitadel-signed JWT access tokens (RS256, verified against the
    # issuer's JWKS) and get_current_user resolves/upserts the real user.
    # Coexists with OPENSWEEP_AUTH_TOKEN (internal/executor clients keep using it).
    # ZITADEL_ISSUER: public issuer URL, no trailing slash — e.g.
    #   https://auth.example.com (prod) or http://localhost:8300 (dev).
    ZITADEL_ISSUER: str = ""
    # SPA application client id — accepted audience for browser tokens.
    ZITADEL_CLIENT_ID: str = ""
    # Project id — also an accepted audience, and selects the per-project
    # roles claim. Optional but recommended.
    ZITADEL_PROJECT_ID: str = ""
    # Optional docker-network URL for JWKS fetches when the public issuer URL
    # isn't reachable from inside the container (dev: http://opensweep_zitadel:8080).
    # Requests carry a Host header derived from ZITADEL_ISSUER so Zitadel's
    # instance resolution still matches.
    ZITADEL_INTERNAL_URL: str = ""

    # First-login org membership (F5/H3). By default a brand-new user always
    # gets their OWN personal org. Legacy/migrated instances that provisioned
    # Organization nodes keyed by the Zitadel resourceowner id (the phase-2
    # scheme, where the IdP org WAS the OpenSweep org) can opt back into
    # "colleagues from the same IdP org land together" by setting this True.
    # Left off, the IdP `resourceowner:id` claim can never place a new user
    # into an existing tenant — closing the org-join seam. Even when on, a
    # joiner's in-org capability role is NEVER taken from the token.
    OPENSWEEP_ALLOW_IDP_ORG_JOIN: bool = False

    # Writable sandbox workspace for disposable agent inspection clones.
    # Inside-container paths the backend writes to:
    OPENSWEEP_SANDBOX_HOST_MOUNT: str = "/host/sandboxes"
    # User-facing host paths (shown in UI for cd-ability):
    OPENSWEEP_SANDBOX_HOST_PATH: str = "~/.opensweep/sandboxes"

    # codebase-memory-mcp binary for per-workspace code-graph tools
    # (KNOWLEDGE_V3_CODE_GRAPH.md). Empty = resolve from PATH; missing binary
    # just disables the code graph — always optional.
    OPENSWEEP_CODE_GRAPH_BIN: str = ""

    # GitHub auth — two paths, with explicit precedence (§7):
    #
    #   1. GitHub App (recommended): created per environment by
    #      scripts/github-app-setup.sh (manifest flow, one browser click)
    #      and configured HERE — dev via .env, prod via terraform.tfvars →
    #      Coolify env (deployment/terraform). See
    #      infrastructure/github_app_store.py. Repos covered by an
    #      installation authenticate with short-lived installation tokens.
    #   2. PAT (GITHUB_TOKEN): the fallback for repos without an installation
    #      and for App-less deployments. When neither is set, GitHubService
    #      falls back to the mock store.
    #
    # The private key comes from GITHUB_APP_PRIVATE_KEY (raw PEM or base64 of
    # one — base64 is what the setup script writes, it survives env transport
    # unmangled) or, alternatively, GITHUB_PRIVATE_KEY_PATH (a mounted PEM
    # file). GITHUB_APP_SLUG builds install URLs (github.com/apps/{slug});
    # the webhook secret is GITHUB_WEBHOOK_SECRET below.
    GITHUB_TOKEN: str = ""
    GITHUB_APP_ID: str = ""
    GITHUB_APP_SLUG: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_PRIVATE_KEY_PATH: str = ""

    # Delivery (PLATFORM_V2_DESIGN.md §5, §7) — webhook HMAC secret and the
    # single commit-status context OpenSweep publishes. When a GitHub App is
    # connected its manifest-issued webhook secret is tried FIRST; this env
    # secret remains accepted as a fallback (manual repo webhooks / migration).
    # Webhooks are rejected when no secret is configured at all.
    GITHUB_WEBHOOK_SECRET: str = ""
    OPENSWEEP_CONVERGED_STATUS_CONTEXT: str = "opensweep/converged"
    # Review outcome status (pending → success|failure|error), published at
    # dispatch, verdict submission, and verification finalize.
    OPENSWEEP_REVIEW_STATUS_CONTEXT: str = "opensweep/review"
    # Auto-review on PR open/sync is a PER-REPO choice now:
    # repository.workflow["review"]["auto"] (domains/repositories/services/workflow.py).

    # Static-analysis candidates injected into review/ask run context (§E):
    # per-tool wall clock and the cap on candidates rendered into the prompt
    # (full output always lands in the artifact store).
    OPENSWEEP_ANALYZER_TIMEOUT_SECONDS: int = 120
    OPENSWEEP_ANALYZER_MAX_CANDIDATES: int = 40

    # ── Slack (per-org workspace integration) ───────────────────────────────
    # One platform-level Slack app serves every tenant; each org installs it
    # into its own workspace via OAuth v2 (api/v1/slack.py). All three must be
    # set for the integration to be available; SLACK_SIGNING_SECRET verifies
    # inbound Events API / slash-command requests. The app needs these
    # request URLs configured (on OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL):
    #   OAuth redirect:  /api/v1/slack/oauth/callback
    #   Event subscriptions (app_mention, message.im): /api/v1/slack/events
    #   Slash command /opensweep: /api/v1/slack/commands
    SLACK_CLIENT_ID: str = ""
    SLACK_CLIENT_SECRET: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # Where browsers should land after backend-handled redirects (GitHub App
    # callback). Empty = relative redirect, correct when frontend and backend
    # share an origin (Caddy prod). Set to the SPA origin when the backend is
    # reached on its own origin (dev split ports, tunnels).
    OPENSWEEP_FRONTEND_BASE_URL: str = ""

    # Public base URL where external systems (Webhook executor targets) reach OpenSweep.
    # On a localhost-bound dev setup this is unreachable from the public internet;
    # use ngrok / cloudflared and override this var to enable WebhookExecutor.
    OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL: str = "http://127.0.0.1:8001"

    # Quota pause/resume (PLATFORM_V2_DESIGN.md §8): a run paused on a
    # provider usage/rate limit is retried after the reset window (unless an
    # unexhausted fallback provider exists — then it retries on the next beat
    # tick), at most OPENSWEEP_QUOTA_MAX_RETRIES times before failing for real.
    OPENSWEEP_QUOTA_RETRY_MINUTES: int = 30
    OPENSWEEP_QUOTA_MAX_RETRIES: int = 6

    # Stuck-run repair: a queued/running run whose transcript stream has been
    # silent this long is failed by the reconciler regardless of provider
    # kind — local providers have no wall ceiling, so liveness is their only
    # backstop. Generous by default: slow local models can sit in one
    # generation for minutes without emitting an event.
    OPENSWEEP_RUN_LIVENESS_TIMEOUT_SECONDS: int = 900

    # Schema migrations (migrations/ + infrastructure/migration_runner.py).
    # AUTO_ROLLBACK: when an older image boots against a database that newer
    # migrations already touched (Coolify deployment rollback), revert those
    # migrations using their DOWN statements stored in the database. Off =
    # refuse to start instead. Migration failures always abort startup so an
    # unhealthy deploy never serves a half-migrated graph.
    OPENSWEEP_MIGRATIONS_AUTO_ROLLBACK: bool = True
    OPENSWEEP_MIGRATIONS_LOCK_TIMEOUT_SECONDS: int = 180

    # Workspace (sandbox) lifecycle — V3 §7: ONE sliding retention window
    # for every purpose (discovery and write). The clock restarts on each
    # turn: cleanup_after = run.last_activity_at + retention. Default 7 days.
    OPENSWEEP_WORKSPACE_RETENTION_HOURS: int = 168
    # Clone depth for sandbox clones from GitHub. Review-runs execute
    # `git diff base...head`, which needs enough history to find a merge-base;
    # 200 commits covers typical PRs. 0 = full clone for pathological cases.
    OPENSWEEP_SANDBOX_CLONE_DEPTH: int = 200

    # MCP — two mounts:
    #   MCP_MOUNT_PATH               — curated external read+CRUD surface
    #   MCP_PLATFORM_TOOL_MOUNT_PATH — 7 write tools executors call back into
    MCP_ENABLED: bool = True
    # External curated read/CRUD mount is OFF by default (no consumer yet —
    # see mcp_app.py). Flip to true to mount it at MCP_MOUNT_PATH.
    MCP_EXTERNAL_ENABLED: bool = False
    MCP_MOUNT_PATH: str = "/mcp"
    MCP_PLATFORM_TOOL_MOUNT_PATH: str = "/mcp/platform"
    # Base URL agent subprocesses use to reach the platform MCP mounts.
    # Default is the compose service name; deployments where the backend
    # answers under a different name (Coolify, host-network dev) override it.
    OPENSWEEP_BACKEND_INTERNAL_URL: str = "http://opensweep_backend:8000"

    # ── Open-web research tools (web_search / fetch_url) ────────────────────
    # SearXNG metasearch base URL for web_search mode="web". Empty (default) =
    # web mode returns {"status": "unavailable"} — github/hackernews/arxiv
    # modes keep working without it. Compose deployments run the bundled
    # opensweep_searxng service: SEARXNG_URL=http://opensweep_searxng:8080
    SEARXNG_URL: str = ""
    # fetch_url body cap (bytes) — pages are truncated, never rejected.
    OPENSWEEP_WEB_FETCH_MAX_BYTES: int = 1_500_000
    # Per-request timeout (seconds) for all outbound web-tool HTTP calls.
    OPENSWEEP_WEB_TOOL_TIMEOUT_SECONDS: int = 20

    # Artifact retention — raw executor output for every Run.
    ARTIFACT_STORE_ROOT: str = "var/artifacts"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Computed
    @property
    def NEO4J_BOLT_URL(self) -> str:
        return f"bolt://{self.NEO4J_USER}:{self.NEO4J_PASSWORD}@{self.NEO4J_HOST}:{self.NEO4J_PORT}"


def _apply_legacy_koala_env() -> None:
    """One-release compat: map Koala-era ``KOALA_*`` env vars onto their
    ``OPENSWEEP_*`` successors when the new name is unset, warning loudly so
    operators migrate their env panels. Remove after the rebrand transition
    window."""
    import os
    import warnings
    from pathlib import Path

    # Pydantic reads .env directly, so legacy names there need mapping too.
    dotenv: dict[str, str] = {}
    env_file = Path(".env")
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            dotenv[k.strip()] = v.strip().strip("'\"")

    merged = {**dotenv, **os.environ}
    known = {k.upper() for k in merged}
    for key, value in merged.items():
        if not key.upper().startswith("KOALA_"):
            continue
        successor = "OPENSWEEP_" + key[len("KOALA_"):]
        if successor.upper() in known:
            continue
        os.environ[successor] = value
        warnings.warn(
            f"env var {key} is deprecated after the OpenSweep rebrand; "
            f"set {successor} instead (legacy value applied for now)",
            DeprecationWarning,
            stacklevel=2,
        )


_apply_legacy_koala_env()
settings = Settings()
