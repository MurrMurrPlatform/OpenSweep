"""F8 (LOW) — two hardening fixes.

1. Artifact URI parsing consistency. The org check in api/v1/artifacts.py
   parsed the repository uid from the URI raw, while the file resolver in
   infrastructure/artifact_store.py sanitizes every path segment with `_safe`.
   Two independent parsers of the same URI is fragile — the checked identity
   and the served path could diverge. A single authoritative
   `artifact_store.repository_uid_of()` (applying the same `_safe`) removes the
   divergence.

2. JWKS `kid` handling. Keys were stored under `jwk.get("kid","")`, so a
   token with no `kid` resolved to whatever landed in the `""` slot. An
   empty/absent `kid` must never resolve a signing key.
"""

import pytest

import infrastructure.oidc as oidc
from infrastructure import artifact_store

pytestmark = pytest.mark.asyncio


def test_repository_uid_of_matches_served_path_segment():
    # A repo uid containing a char `_safe` rewrites: the checked identity must
    # equal the segment used to build the on-disk path, not the raw token.
    uri = "opensweep-artifact://repo@x/run1/raw.txt"
    assert artifact_store.repository_uid_of(uri) == artifact_store._safe("repo@x")


def test_repository_uid_of_normal_uid_roundtrips():
    uri = "opensweep-artifact://repo-a/run1/raw.txt"
    assert artifact_store.repository_uid_of(uri) == "repo-a"


def test_repository_uid_of_rejects_foreign_scheme():
    assert artifact_store.repository_uid_of("http://evil/repo-a/x") == ""


async def test_empty_kid_never_resolves_a_key():
    # Even if a keyless entry somehow existed, an empty `kid` must not match it.
    saved = oidc._keys
    oidc._keys = {"": object()}
    try:
        assert await oidc._signing_key("") is None
    finally:
        oidc._keys = saved
