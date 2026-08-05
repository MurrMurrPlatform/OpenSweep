"""Startup guards — refuse to boot a footgun configuration.

Pure functions over a settings-shaped object so tests can drive them with a
SimpleNamespace. Called from app.py's lifespan (OUTSIDE any try/except — a
misconfigured deploy must go unhealthy, same rationale as the migration
block) and from celery_app.init_worker (log critical + exit(1)).

Three tiers: `auth_config_errors` applies in EVERY environment (Zitadel OIDC
is the only supported user auth — see deployment/ZITADEL.md);
`deployed_config_errors` bites any non-local environment (production OR
staging); `production_config_*` checks only bite when ENVIRONMENT is
production.

Deliberately NOT config.py validators: those run at import time and would
break scripts/tests that import config with a partial environment.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_production(env: str) -> bool:
    return (env or "").strip().lower() in ("production", "prod")


def is_deployed(env: str) -> bool:
    """Any environment that isn't a throwaway local/dev/test workstation —
    a superset of production (staging, prod, …)."""
    return (env or "").strip().lower() not in ("", "local", "dev", "development", "test")


# Neo4j credentials shipped as defaults (config.py / docker-compose): acceptable
# on a local stack, never on a deployed instance.
_DEFAULT_NEO4J_PASSWORDS = {"opensweeppassword", "koalapassword"}


def deployed_config_errors(s) -> list[str]:
    """Hard errors for any deployed (non-local) environment (F8).

    Broader than `production_config_errors`, which only bites
    ENVIRONMENT=production — staging deserves the same hard checks, not just
    production, for footguns that are equally misleading there."""
    if not is_deployed(getattr(s, "ENVIRONMENT", "")):
        return []
    errors: list[str] = []
    if getattr(s, "NEO4J_PASSWORD", "") in _DEFAULT_NEO4J_PASSWORDS:
        errors.append(
            "ENVIRONMENT is deployed (non-local) but NEO4J_PASSWORD is still a "
            "well-known default ('opensweeppassword' or 'koalapassword'). Set a "
            "strong NEO4J_PASSWORD (and update the Neo4j container credentials to "
            "match)."
        )

    # Role-claim pin (F5): zitadel_roles() (infrastructure/oidc.py) only reads
    # roles project-agnostically when ZITADEL_PROJECT_ID is unset — a foreign
    # project's 'admin' role on the same issuer then confers OpenSweep
    # platform-admin. ZITADEL_CLIENT_ID alone does NOT gate this: it only pins
    # the JWT audience (_audience_ok), never reaches zitadel_roles/
    # map_opensweep_role/is_platform_admin_claim. So this specifically
    # requires ZITADEL_PROJECT_ID, not "either identifier".
    zitadel_issuer = (getattr(s, "ZITADEL_ISSUER", "") or "").strip()
    if zitadel_issuer:
        project_id = (getattr(s, "ZITADEL_PROJECT_ID", "") or "").strip()
        if not project_id:
            errors.append(
                "ENVIRONMENT is deployed and ZITADEL_ISSUER is set, but "
                "ZITADEL_PROJECT_ID is not configured — role claims (viewer/"
                "maintainer/admin) are read project-agnostically "
                "(infrastructure/oidc.py zitadel_roles), so a foreign project's "
                "'admin' role on the same issuer confers OpenSweep platform-admin "
                "regardless of ZITADEL_CLIENT_ID. Set ZITADEL_PROJECT_ID "
                "(ZITADEL_CLIENT_ID alone only pins the audience, not the roles "
                "claim)."
            )

    # Short-key check (F-secrets): secretbox silently treats a <16-char key as
    # unconfigured and stores secrets in PLAINTEXT — equally misleading in
    # staging as in production, so this belongs here rather than
    # production_config_errors.
    secrets_key = (getattr(s, "OPENSWEEP_SECRETS_KEY", "") or "").strip()
    if secrets_key and len(secrets_key) < 16:
        errors.append(
            "OPENSWEEP_SECRETS_KEY is set but shorter than 16 characters — "
            "secretbox treats it as unconfigured and secrets would be stored "
            "in PLAINTEXT despite the key being set. Use at least 16 chars "
            "(e.g. `openssl rand -hex 32`)."
        )
    return errors


def auth_config_errors(s) -> list[str]:
    """Hard errors in EVERY environment.

    Zitadel OIDC login is the only supported user authentication. Booting
    without it would silently serve every request as the hardcoded
    platform-admin local user (or, with only OPENSWEEP_AUTH_TOKEN set, leave
    browsers with no login at all) — modes the product no longer supports.
    The no-auth code path in TokenAuthMiddleware survives purely for unit
    tests; no bootable configuration reaches it.
    """
    zitadel_issuer = (getattr(s, "ZITADEL_ISSUER", "") or "").strip()
    if zitadel_issuer:
        return []
    return [
        "ZITADEL_ISSUER is empty — Zitadel OIDC login is the only supported "
        "user authentication. Dev: `docker compose up -d` (Zitadel is part of "
        "the default stack) then run `scripts/zitadel-dev-setup.sh` once — "
        "it configures Zitadel and writes ZITADEL_*/VITE_ZITADEL_* into .env. "
        "Prod: point ZITADEL_ISSUER at your instance (deployment/ZITADEL.md). "
        "OPENSWEEP_AUTH_TOKEN is service-to-service auth only and does not "
        "replace user login."
    ]


def production_config_errors(s) -> list[str]:
    """Hard errors — booting like this in production is unacceptable."""
    errors: list[str] = []
    if not is_production(getattr(s, "ENVIRONMENT", "")):
        return errors

    auth_token = (getattr(s, "OPENSWEEP_AUTH_TOKEN", "") or "").strip()
    zitadel_issuer = (getattr(s, "ZITADEL_ISSUER", "") or "").strip()

    if not auth_token and not zitadel_issuer:
        errors.append(
            "ENVIRONMENT is production but no authentication is configured: "
            "with both OPENSWEEP_AUTH_TOKEN and ZITADEL_ISSUER empty, EVERY request "
            "is served unauthenticated as the hardcoded platform-admin local "
            "user (domains/users/services/local_user.py). Fix: set "
            "OPENSWEEP_AUTH_TOKEN (e.g. `openssl rand -hex 32`) or configure "
            "ZITADEL_ISSUER for OIDC login."
        )

    # NEO4J default-password check, the ZITADEL_PROJECT_ID role-claim pin, and
    # the OPENSWEEP_SECRETS_KEY short-key check all live in
    # deployed_config_errors (fires for production AND staging), wired into
    # enforce_production_guards below.

    return errors


def production_config_warnings(s) -> list[str]:
    """Soft warnings — boot proceeds, but the operator should fix these."""
    warnings: list[str] = []
    if not is_production(getattr(s, "ENVIRONMENT", "")):
        return warnings

    if not (getattr(s, "OPENSWEEP_SECRETS_KEY", "") or "").strip():
        warnings.append(
            "OPENSWEEP_SECRETS_KEY is empty in production — provider credentials "
            "and GitHub App secrets are stored in PLAINTEXT at rest. Set "
            "OPENSWEEP_SECRETS_KEY to encrypt secrets on disk/in the graph."
        )

    if not (getattr(s, "OPENSWEEP_STATE_SIGNING_SECRET", "") or "").strip():
        warnings.append(
            "OPENSWEEP_STATE_SIGNING_SECRET is empty in production — GitHub App "
            "state-nonce signing falls back to the API auth token. Set a "
            "dedicated OPENSWEEP_STATE_SIGNING_SECRET."
        )

    return warnings


def enforce_production_guards(s) -> None:
    """Log every warning; raise RuntimeError joining all hard errors."""
    for warning in production_config_warnings(s):
        logger.warning(warning)
    errors = (
        auth_config_errors(s)
        + deployed_config_errors(s)
        + production_config_errors(s)
    )
    if errors:
        raise RuntimeError("\n".join(errors))
