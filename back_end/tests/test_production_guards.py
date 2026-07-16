"""Production startup guards — pure tests, no DB (infrastructure/production_guards.py)."""

from types import SimpleNamespace

import pytest

from infrastructure.production_guards import (
    auth_config_errors,
    enforce_production_guards,
    is_production,
    production_config_errors,
    production_config_warnings,
)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ENVIRONMENT="local",
        OPENSWEEP_AUTH_TOKEN="",
        ZITADEL_ISSUER="",
        NEO4J_PASSWORD="opensweeppassword",
        OPENSWEEP_SECRETS_KEY="",
        OPENSWEEP_STATE_SIGNING_SECRET="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _prod(**overrides) -> SimpleNamespace:
    """A fully hardened production config; overrides poke holes."""
    base = dict(
        ENVIRONMENT="production",
        OPENSWEEP_AUTH_TOKEN="a-real-token",
        ZITADEL_ISSUER="",
        # Audience pin (F5): a hardened config that enables Zitadel also pins
        # the accepted audience, so enabling the issuer alone stays clean.
        ZITADEL_CLIENT_ID="spa-app-id",
        ZITADEL_PROJECT_ID="",
        NEO4J_PASSWORD="a-strong-password",
        OPENSWEEP_SECRETS_KEY="a-secrets-key-16-chars-plus",
        OPENSWEEP_STATE_SIGNING_SECRET="a-state-secret",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_is_production_normalization():
    assert is_production("production")
    assert is_production("prod")
    assert is_production("PRODUCTION")
    assert is_production("  Prod  ")
    assert not is_production("local")
    assert not is_production("staging")
    assert not is_production("")
    assert not is_production(None)


def test_local_defaults_skip_production_checks_but_require_zitadel():
    s = _settings()
    assert production_config_errors(s) == []
    assert production_config_warnings(s) == []
    # Zitadel is required in EVERY environment — local defaults no longer boot.
    with pytest.raises(RuntimeError, match="ZITADEL_ISSUER"):
        enforce_production_guards(s)
    enforce_production_guards(
        _settings(ZITADEL_ISSUER="http://localhost:8300")
    )  # must not raise


def test_auth_config_errors_every_environment():
    for env in ("local", "staging", "production", ""):
        errors = auth_config_errors(_settings(ENVIRONMENT=env))
        assert len(errors) == 1
        assert "ZITADEL_ISSUER" in errors[0]
        assert "zitadel-dev-setup.sh" in errors[0]  # names the dev fix


def test_auth_config_clean_with_issuer():
    assert auth_config_errors(_settings(ZITADEL_ISSUER="https://auth.example.com")) == []
    assert auth_config_errors(_settings(ZITADEL_ISSUER="  ")) != []  # whitespace = empty


def test_auth_token_does_not_satisfy_user_auth():
    # OPENSWEEP_AUTH_TOKEN is service-to-service only — it must not silence the guard.
    assert auth_config_errors(_settings(OPENSWEEP_AUTH_TOKEN="a-real-token")) != []


def test_production_without_auth_is_error_a():
    errors = production_config_errors(_prod(OPENSWEEP_AUTH_TOKEN="", ZITADEL_ISSUER=""))
    assert len(errors) == 1
    assert "OPENSWEEP_AUTH_TOKEN" in errors[0]
    assert "ZITADEL_ISSUER" in errors[0]
    assert "local_user" in errors[0]  # names the served-as-admin footgun


def test_production_whitespace_auth_token_still_error_a():
    errors = production_config_errors(_prod(OPENSWEEP_AUTH_TOKEN="   ", ZITADEL_ISSUER=" "))
    assert len(errors) == 1


def test_zitadel_alone_satisfies_auth():
    s = _prod(OPENSWEEP_AUTH_TOKEN="", ZITADEL_ISSUER="https://auth.example.com")
    assert production_config_errors(s) == []


def test_production_default_neo4j_password_is_error_b_only():
    errors = production_config_errors(_prod(NEO4J_PASSWORD="opensweeppassword"))
    assert len(errors) == 1
    assert "NEO4J_PASSWORD" in errors[0]


def test_production_koalapassword_is_also_rejected():
    errors = production_config_errors(_prod(NEO4J_PASSWORD="koalapassword"))
    assert len(errors) == 1
    assert "NEO4J_PASSWORD" in errors[0]


def test_zitadel_issuer_without_audience_pin_blocks_boot():
    # F5: an issuer set but no client/project id = accept-any-audience → refuse.
    errors = production_config_errors(
        _prod(ZITADEL_ISSUER="https://auth.example.com", ZITADEL_CLIENT_ID="", ZITADEL_PROJECT_ID="")
    )
    assert any("ZITADEL_CLIENT_ID" in e or "ZITADEL_PROJECT_ID" in e for e in errors)
    # Either identifier alone satisfies the pin.
    assert production_config_errors(
        _prod(ZITADEL_ISSUER="https://auth.example.com", ZITADEL_CLIENT_ID="", ZITADEL_PROJECT_ID="proj-1")
    ) == []


def test_production_fully_configured_is_clean():
    s = _prod(ZITADEL_ISSUER="https://auth.example.com")
    assert production_config_errors(s) == []
    assert production_config_warnings(s) == []
    enforce_production_guards(s)


def test_casing_variants_trigger_errors():
    for env in ("prod", "PRODUCTION", "Prod"):
        assert production_config_errors(_prod(ENVIRONMENT=env, OPENSWEEP_AUTH_TOKEN=""))


def test_enforce_raises_with_all_messages_joined():
    s = _prod(OPENSWEEP_AUTH_TOKEN="", ZITADEL_ISSUER="", NEO4J_PASSWORD="opensweeppassword")
    with pytest.raises(RuntimeError) as exc_info:
        enforce_production_guards(s)
    text = str(exc_info.value)
    assert "OPENSWEEP_AUTH_TOKEN" in text
    assert "NEO4J_PASSWORD" in text
    assert text.count("\n") >= 1  # two errors, newline-joined


def test_warnings_only_in_production():
    s = _settings(OPENSWEEP_SECRETS_KEY="", OPENSWEEP_STATE_SIGNING_SECRET="")
    assert production_config_warnings(s) == []

    warnings = production_config_warnings(_prod(OPENSWEEP_SECRETS_KEY=""))
    assert len(warnings) == 1
    assert "OPENSWEEP_SECRETS_KEY" in warnings[0]

    warnings = production_config_warnings(_prod(OPENSWEEP_STATE_SIGNING_SECRET=""))
    assert len(warnings) == 1
    assert "OPENSWEEP_STATE_SIGNING_SECRET" in warnings[0]

    both = production_config_warnings(
        _prod(OPENSWEEP_SECRETS_KEY="", OPENSWEEP_STATE_SIGNING_SECRET="")
    )
    assert len(both) == 2


def test_warnings_never_raise(caplog):
    s = _prod(
        ZITADEL_ISSUER="https://auth.example.com",
        OPENSWEEP_SECRETS_KEY="",
        OPENSWEEP_STATE_SIGNING_SECRET="",
    )
    enforce_production_guards(s)  # warnings logged, no raise


def test_settings_object_missing_optional_fields():
    """Warnings use getattr with defaults — a settings object predating the
    new fields must not crash the guard."""
    s = SimpleNamespace(
        ENVIRONMENT="production",
        OPENSWEEP_AUTH_TOKEN="tok",
        ZITADEL_ISSUER="",
        NEO4J_PASSWORD="strong",
    )
    assert production_config_errors(s) == []
    assert len(production_config_warnings(s)) == 2


def test_short_secrets_key_is_a_hard_error_in_production():
    errors = production_config_errors(_prod(OPENSWEEP_SECRETS_KEY="tooshort"))
    assert len(errors) == 1
    assert "shorter than 16 characters" in errors[0]


def test_short_secrets_key_ignored_outside_production():
    assert production_config_errors(_settings(OPENSWEEP_SECRETS_KEY="tooshort")) == []
