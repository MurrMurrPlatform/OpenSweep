"""Startup guards — refuse to boot a footgun configuration.

Pure functions over a settings-shaped object so tests can drive them with a
SimpleNamespace. Called from app.py's lifespan (OUTSIDE any try/except — a
misconfigured deploy must go unhealthy, same rationale as the migration
block) and from celery_app.init_worker (log critical + exit(1)).

Two tiers: `auth_config_errors` applies in EVERY environment (Zitadel OIDC
is the only supported user auth — see deployment/ZITADEL.md), while the
`production_config_*` checks only bite when ENVIRONMENT is production.

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

    A well-known default DB password is fine on a disposable local stack but
    must never reach a deployed instance — production OR staging. Broader than
    `production_config_errors`, which only bites ENVIRONMENT=production."""
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

    # Audience pin (F5): with an issuer but no client/project id, OIDC accepts
    # ANY audience from the issuer — a token minted for an unrelated app on the
    # same Zitadel is honored, and its roles could confer platform-admin.
    # Refuse to boot a production instance in that accept-any state.
    if zitadel_issuer:
        client_id = (getattr(s, "ZITADEL_CLIENT_ID", "") or "").strip()
        project_id = (getattr(s, "ZITADEL_PROJECT_ID", "") or "").strip()
        if not client_id and not project_id:
            errors.append(
                "ENVIRONMENT is production and ZITADEL_ISSUER is set, but neither "
                "ZITADEL_CLIENT_ID nor ZITADEL_PROJECT_ID is configured — OIDC would "
                "accept tokens for ANY audience on the issuer (and a foreign "
                "project's 'admin' role could confer platform-admin). Set "
                "ZITADEL_CLIENT_ID (the SPA app id) and/or ZITADEL_PROJECT_ID to pin "
                "the accepted audience to this OpenSweep instance."
            )

    if not auth_token and not zitadel_issuer:
        errors.append(
            "ENVIRONMENT is production but no authentication is configured: "
            "with both OPENSWEEP_AUTH_TOKEN and ZITADEL_ISSUER empty, EVERY request "
            "is served unauthenticated as the hardcoded platform-admin local "
            "user (domains/users/services/local_user.py). Fix: set "
            "OPENSWEEP_AUTH_TOKEN (e.g. `openssl rand -hex 32`) or configure "
            "ZITADEL_ISSUER for OIDC login."
        )

    # NEO4J default-password check lives in deployed_config_errors (fires for
    # production AND staging), wired into enforce_production_guards below.

    secrets_key = (getattr(s, "OPENSWEEP_SECRETS_KEY", "") or "").strip()
    if secrets_key and len(secrets_key) < 16:
        # Worse than no key: the operator set one expecting encryption, but
        # secretbox treats short keys as unconfigured and writes PLAINTEXT.
        errors.append(
            "OPENSWEEP_SECRETS_KEY is set but shorter than 16 characters — "
            "secretbox treats it as unconfigured and secrets would be stored "
            "in PLAINTEXT despite the key being set. Use at least 16 chars "
            "(e.g. `openssl rand -hex 32`)."
        )

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
