"""F5 (HIGH) — platform-admin and audience must be pinned to OpenSweep's project.

WHY: two coupled weaknesses in the OIDC path, both bootable in production
because the guards only required ZITADEL_ISSUER:
  1. `zitadel_roles` harvested the `admin` role from ANY project's roles claim,
     so a user holding `admin` on an unrelated app sharing the same Zitadel
     issuer became the OpenSweep instance operator (is_platform_admin).
  2. With neither ZITADEL_CLIENT_ID nor ZITADEL_PROJECT_ID configured, tokens
     were accepted for any audience — a prod deployment could boot in that
     accept-any state.

WHAT:
  - `zitadel_roles(claims, project_id=...)` only reads the generic roles claim
    plus the claim scoped to OpenSweep's own project id — a foreign project's
    `admin` no longer grants platform-admin.
  - `production_config_errors` refuses to boot production when ZITADEL_ISSUER is
    set but no client/project id pins the accepted audience.
Pure functions — no DB.
"""

from types import SimpleNamespace

from infrastructure.oidc import zitadel_roles
from infrastructure.production_guards import production_config_errors

_OUR_PROJECT = "1234"


def test_foreign_project_admin_role_is_not_granted_when_pinned():
    # `admin` asserted only in a DIFFERENT project's roles claim.
    claims = {"urn:zitadel:iam:org:project:9999:roles": {"admin": {"o": "org"}}}
    assert "admin" not in zitadel_roles(claims, project_id=_OUR_PROJECT)


def test_our_project_admin_role_is_granted():
    claims = {f"urn:zitadel:iam:org:project:{_OUR_PROJECT}:roles": {"admin": {"o": "org"}}}
    assert "admin" in zitadel_roles(claims, project_id=_OUR_PROJECT)


def test_generic_roles_claim_still_read_when_pinned():
    # Zitadel puts the primary audience project's roles in the generic claim.
    claims = {"urn:zitadel:iam:org:project:roles": {"maintainer": {"o": "org"}}}
    assert "maintainer" in zitadel_roles(claims, project_id=_OUR_PROJECT)


def test_unpinned_behavior_preserved_for_dev():
    # No project configured (dev) → back-compat: any project's roles count.
    claims = {"urn:zitadel:iam:org:project:9999:roles": {"admin": {"o": "org"}}}
    assert "admin" in zitadel_roles(claims)


def _prod(**over):
    base = dict(
        ENVIRONMENT="production",
        OPENSWEEP_AUTH_TOKEN="tok",
        ZITADEL_ISSUER="https://auth.example.com",
        ZITADEL_CLIENT_ID="",
        ZITADEL_PROJECT_ID="",
        NEO4J_PASSWORD="a-strong-password",
        OPENSWEEP_SECRETS_KEY="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_prod_boot_blocked_without_audience_pin():
    errors = production_config_errors(_prod())
    assert any("ZITADEL_CLIENT_ID" in e or "ZITADEL_PROJECT_ID" in e for e in errors)


def test_prod_boot_ok_with_client_id():
    errors = production_config_errors(_prod(ZITADEL_CLIENT_ID="spa-abc"))
    assert not any("ZITADEL_CLIENT_ID" in e or "ZITADEL_PROJECT_ID" in e for e in errors)


def test_audience_pin_not_required_when_issuer_unset():
    # A no-Zitadel prod (shared-token only) must not trip the new guard.
    errors = production_config_errors(_prod(ZITADEL_ISSUER=""))
    assert not any("ZITADEL_CLIENT_ID" in e or "ZITADEL_PROJECT_ID" in e for e in errors)
