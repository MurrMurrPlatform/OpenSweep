"""Secrets encryption at rest — `enc:v1:<fernet-token>`.

Sealed values carry the `enc:v1:` prefix; anything without an `enc:` prefix
is legacy plaintext and loads forever (unseal is a passthrough for it).

Key handling:
  - OPENSWEEP_SECRETS_KEY — any string >= 16 chars; the Fernet key is derived as
    base64.urlsafe_b64encode(sha256(raw)). Losing it makes sealed secrets
    unrecoverable.
  - OPENSWEEP_SECRETS_KEY_FALLBACKS — comma-separated previous raw keys kept
    during rotation. Encryption always uses the primary; decryption tries
    primary then each fallback (MultiFernet).

Fail-closed: a sealed value that cannot be decrypted (missing/wrong key)
raises SecretBoxError — ciphertext is NEVER returned as if it were the
secret. Sealing without a key degrades to plaintext with a logged warning
(ERROR-level in production) so a fresh dev setup keeps working.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger(__name__)

# enc:v2: derives the Fernet key with scrypt (salted, work-factored) instead of
# a bare unsalted SHA-256 (enc:v1:), which was trivially brute-forceable offline
# for a passphrase-shaped key. New seals are v2; v1 stays decryptable so old
# ciphertext loads and can be rotated up.
PREFIX = "enc:v2:"
PREFIX_V1 = "enc:v1:"
_SEALED_PREFIXES = (PREFIX, PREFIX_V1)
_MIN_KEY_LEN = 16

# Fixed application salt used when OPENSWEEP_SECRETS_SALT is unset — still gives
# scrypt's work factor + domain separation; a configured salt adds per-deploy
# uniqueness. scrypt params: ~64 MiB, a fraction of a second (derivations are
# cached, so this runs a handful of times, not per unseal).
_DEFAULT_SALT = b"opensweep.secretbox.v2"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class SecretBoxError(RuntimeError):
    """A sealed secret could not be decrypted — fail closed."""


def _derive_v1(raw: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def _derive_v2(raw: str, salt: bytes) -> Fernet:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(raw.encode())))


def _current_keys() -> tuple[str, tuple[str, ...], bytes]:
    """Read settings lazily each call so tests can monkeypatch them."""
    from config import settings

    primary = (getattr(settings, "OPENSWEEP_SECRETS_KEY", "") or "").strip()
    fallbacks = tuple(
        k.strip()
        for k in (getattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", "") or "").split(",")
        if k.strip()
    )
    raw_salt = (getattr(settings, "OPENSWEEP_SECRETS_SALT", "") or "").strip()
    salt = raw_salt.encode() if raw_salt else _DEFAULT_SALT
    return primary, fallbacks, salt


# (primary, fallbacks, salt) → (MultiFernet, primary v2 Fernet). One entry: keys
# only change on config edits (or test monkeypatching — a changed tuple rebuilds).
_cache: dict[tuple[str, tuple[str, ...], bytes], tuple[MultiFernet, Fernet]] = {}


def _reset_cache() -> None:
    _cache.clear()


def _boxes() -> tuple[MultiFernet, Fernet] | None:
    """The (decrypt MultiFernet, encrypt Fernet) pair, or None when unconfigured.

    Encryption uses the v2 (scrypt) key. The MultiFernet holds BOTH the v2 and
    v1 derivations of the primary and every fallback, so any historical
    ciphertext (v1 or v2, primary or rotated) still decrypts and can be upgraded.
    """
    primary, fallbacks, salt = _current_keys()
    if not primary:
        return None
    if len(primary) < _MIN_KEY_LEN:
        logger.warning(
            f"OPENSWEEP_SECRETS_KEY is shorter than {_MIN_KEY_LEN} chars — ignored; "
            "secrets encryption stays OFF"
        )
        return None
    key = (primary, fallbacks, salt)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    encrypt_fernet = _derive_v2(primary, salt)
    usable = [primary] + [f for f in fallbacks if len(f) >= _MIN_KEY_LEN]
    # v2 first (the common case now), then v1 for legacy ciphertext. MultiFernet
    # tries each in order until one decrypts.
    fernets = [encrypt_fernet]
    fernets += [_derive_v2(f, salt) for f in usable[1:]]
    fernets += [_derive_v1(k) for k in usable]
    entry = (MultiFernet(fernets), encrypt_fernet)
    _cache.clear()
    _cache[key] = entry
    return entry


def configured() -> bool:
    """True when a usable OPENSWEEP_SECRETS_KEY is set."""
    return _boxes() is not None


def is_sealed(value: str) -> bool:
    return (value or "").startswith(_SEALED_PREFIXES)


def _strip_prefix(value: str) -> str:
    """The Fernet token after a known enc:vN: prefix (both are 7 chars)."""
    for p in _SEALED_PREFIXES:
        if value.startswith(p):
            return value[len(p):]
    return value


def seal(plaintext: str) -> str:
    """Encrypt under the primary key. '' → ''; already-sealed → as-is;
    no key configured → plaintext back, with a logged warning (ERROR in
    production) — writes must not break an unconfigured deployment."""
    if not plaintext:
        return ""
    if is_sealed(plaintext):
        return plaintext
    boxes = _boxes()
    if boxes is None:
        from config import settings
        from infrastructure.production_guards import is_production

        msg = (
            "OPENSWEEP_SECRETS_KEY is not configured — secret stored in PLAINTEXT "
            "at rest. Set OPENSWEEP_SECRETS_KEY to enable encryption."
        )
        if is_production(getattr(settings, "ENVIRONMENT", "")):
            logger.error(msg)
        else:
            logger.warning(msg)
        return plaintext
    _, primary = boxes
    return PREFIX + primary.encrypt(plaintext.encode()).decode()


def unseal(value: str) -> str:
    """Decrypt a sealed value. No `enc:` prefix → returned as-is (legacy
    plaintext loads forever). Decrypt failure or missing key → SecretBoxError
    (FAIL CLOSED — ciphertext is never returned)."""
    value = value or ""
    if not value.startswith("enc:"):
        return value
    if not is_sealed(value):
        raise SecretBoxError(
            f"sealed secret has an unknown format version ({value.split(':', 2)[:2]}) — "
            "this OpenSweep build understands enc:v1: / enc:v2:. Upgrade the deployment."
        )
    boxes = _boxes()
    if boxes is None:
        raise SecretBoxError(
            "found an encrypted secret (enc:) but OPENSWEEP_SECRETS_KEY is not "
            "configured — set OPENSWEEP_SECRETS_KEY to the key it was sealed with "
            "(and OPENSWEEP_SECRETS_KEY_FALLBACKS for any previous keys)."
        )
    multi, _ = boxes
    try:
        return multi.decrypt(_strip_prefix(value).encode()).decode()
    except InvalidToken as exc:
        raise SecretBoxError(
            "failed to decrypt a sealed secret: OPENSWEEP_SECRETS_KEY (and every "
            "OPENSWEEP_SECRETS_KEY_FALLBACKS entry) does not match the key it was "
            "sealed with. Add the old key to OPENSWEEP_SECRETS_KEY_FALLBACKS to "
            "recover, or re-enter the secret."
        ) from exc


def rotate(value: str) -> str:
    """Re-seal under the primary key iff the value is currently decryptable
    only by a fallback key. Plaintext input is sealed; primary-sealed input
    is returned unchanged (no churn)."""
    value = value or ""
    if not is_sealed(value):
        return seal(value)
    boxes = _boxes()
    if boxes is None:
        raise SecretBoxError(
            "cannot rotate a sealed secret without OPENSWEEP_SECRETS_KEY configured"
        )
    multi, primary = boxes
    token = _strip_prefix(value).encode()
    # Already v2 under the primary key → no churn. A v1 token (even under the
    # current key) is upgraded to v2 below, so it is NOT short-circuited here.
    if value.startswith(PREFIX):
        try:
            primary.decrypt(token)
            return value
        except InvalidToken:
            pass
    try:
        return PREFIX + multi.rotate(token).decode()
    except InvalidToken as exc:
        raise SecretBoxError(
            "failed to rotate a sealed secret: no configured key "
            "(OPENSWEEP_SECRETS_KEY / OPENSWEEP_SECRETS_KEY_FALLBACKS) can decrypt it."
        ) from exc
