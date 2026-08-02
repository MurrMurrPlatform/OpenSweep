"""Secrets encryption at rest (infrastructure/secretbox.py) — pure tests."""

import pytest

from config import settings
from infrastructure import secretbox
from infrastructure.secretbox import SecretBoxError

KEY = "unit-test-primary-key-0123456789"
OLD_KEY = "unit-test-old-key-abcdefghijklmn"


@pytest.fixture(autouse=True)
def clean_box(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "", raising=False)
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", "", raising=False)
    secretbox._reset_cache()
    yield
    secretbox._reset_cache()


def _with_key(monkeypatch, key=KEY, fallbacks=""):
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", key, raising=False)
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", fallbacks, raising=False)
    secretbox._reset_cache()


def test_roundtrip(monkeypatch):
    _with_key(monkeypatch)
    sealed = secretbox.seal("sk-super-secret")
    assert sealed != "sk-super-secret"
    assert secretbox.unseal(sealed) == "sk-super-secret"


def test_sealed_values_carry_v2_prefix(monkeypatch):
    _with_key(monkeypatch)
    sealed = secretbox.seal("x")
    assert sealed.startswith("enc:v2:")  # new seals use the scrypt KDF
    assert secretbox.is_sealed(sealed)
    assert not secretbox.is_sealed("x")
    assert not secretbox.is_sealed("")


def test_seal_is_idempotent_on_sealed_input(monkeypatch):
    _with_key(monkeypatch)
    sealed = secretbox.seal("x")
    assert secretbox.seal(sealed) == sealed


def test_unseal_passthrough_on_plaintext_and_empty(monkeypatch):
    _with_key(monkeypatch)
    assert secretbox.unseal("legacy-plaintext") == "legacy-plaintext"
    assert secretbox.unseal("") == ""
    assert secretbox.unseal(None) == ""


def test_seal_empty_is_empty(monkeypatch):
    _with_key(monkeypatch)
    assert secretbox.seal("") == ""


def test_no_key_seal_returns_plaintext():
    assert not secretbox.configured()
    assert secretbox.seal("sk-plain") == "sk-plain"


def test_sealed_without_key_raises(monkeypatch):
    _with_key(monkeypatch)
    sealed = secretbox.seal("x")
    _with_key(monkeypatch, key="")
    with pytest.raises(SecretBoxError, match="OPENSWEEP_SECRETS_KEY"):
        secretbox.unseal(sealed)


def test_wrong_key_raises(monkeypatch):
    _with_key(monkeypatch, key=OLD_KEY)
    sealed = secretbox.seal("x")
    _with_key(monkeypatch, key=KEY)
    with pytest.raises(SecretBoxError, match="OPENSWEEP_SECRETS_KEY_FALLBACKS"):
        secretbox.unseal(sealed)


def test_fallback_key_decrypts(monkeypatch):
    _with_key(monkeypatch, key=OLD_KEY)
    sealed = secretbox.seal("rotated-secret")
    _with_key(monkeypatch, key=KEY, fallbacks=f" {OLD_KEY} , ")
    assert secretbox.unseal(sealed) == "rotated-secret"


def test_rotate_reseals_under_primary(monkeypatch):
    _with_key(monkeypatch, key=OLD_KEY)
    old_sealed = secretbox.seal("rotate-me")
    _with_key(monkeypatch, key=KEY, fallbacks=OLD_KEY)

    rotated = secretbox.rotate(old_sealed)
    assert rotated != old_sealed
    assert secretbox.unseal(rotated) == "rotate-me"

    # Now decryptable by the primary ALONE (fallback removed).
    _with_key(monkeypatch, key=KEY)
    assert secretbox.unseal(rotated) == "rotate-me"

    # A primary-sealed value is returned unchanged — no churn.
    assert secretbox.rotate(rotated) == rotated


def test_rotate_seals_plaintext(monkeypatch):
    _with_key(monkeypatch)
    out = secretbox.rotate("plain")
    assert out.startswith("enc:v2:")
    assert secretbox.unseal(out) == "plain"


def test_v1_ciphertext_still_decrypts_and_upgrades_to_v2(monkeypatch):
    _with_key(monkeypatch)
    # Forge a legacy enc:v1: token with the old unsalted-SHA-256 derivation.
    v1_fernet = secretbox._derive_v1(KEY)
    v1 = "enc:v1:" + v1_fernet.encrypt(b"legacy-secret").decode()
    # Old ciphertext still loads...
    assert secretbox.unseal(v1) == "legacy-secret"
    # ...and rotate upgrades it to v2 (scrypt), still decryptable.
    rotated = secretbox.rotate(v1)
    assert rotated.startswith("enc:v2:")
    assert secretbox.unseal(rotated) == "legacy-secret"


def test_unknown_version_prefix_raises(monkeypatch):
    _with_key(monkeypatch)
    with pytest.raises(SecretBoxError, match="format"):
        secretbox.unseal("enc:v9:whatever")


def test_short_key_is_treated_as_unconfigured(monkeypatch):
    _with_key(monkeypatch, key="short")
    assert not secretbox.configured()
    assert secretbox.seal("x") == "x"


def test_configured_flag(monkeypatch):
    assert not secretbox.configured()
    _with_key(monkeypatch)
    assert secretbox.configured()
