"""Credential sealing — helpers on stubs, plus the write sites.

Part 1: provider_secret()/sealed_secret() on stub objects — no DB.
Part 2: llm_provider_service create/update write sites and
encrypt_plaintext_provider_secrets(), exercised against the same in-memory
LLMProvider fake test_llm_provider_tenancy.py uses (DB-free — the service
and credentials modules are monkeypatched)."""

from types import SimpleNamespace

import pytest

import domains.llm_providers.services.credentials as credentials_mod
import domains.llm_providers.services.llm_provider_service as svc_mod
from config import settings
from domains.llm_providers.schemas import (
    CreateLLMProviderRequest,
    LLMProviderKind,
    UpdateLLMProviderRequest,
)
from domains.llm_providers.services.credentials import (
    encrypt_plaintext_provider_secrets,
    provider_secret,
    sealed_secret,
)
from domains.llm_providers.services.llm_provider_service import LLMProviderService
from domains.users.schemas import UserDTO
from infrastructure import secretbox
from infrastructure.secretbox import SecretBoxError

KEY = "unit-test-secrets-key-0123456789"


@pytest.fixture
def secrets_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", KEY, raising=False)
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY_FALLBACKS", "", raising=False)
    secretbox._reset_cache()
    yield
    secretbox._reset_cache()


def test_plaintext_credential_passes_through(secrets_key):
    provider = SimpleNamespace(credential_secret="  sk-legacy-plain  ")
    assert provider_secret(provider) == "sk-legacy-plain"


def test_sealed_credential_roundtrips(secrets_key):
    provider = SimpleNamespace(credential_secret=sealed_secret("sk-abc123"))
    assert provider.credential_secret.startswith("enc:v1:")
    assert provider_secret(provider) == "sk-abc123"


def test_empty_and_missing_credential(secrets_key):
    assert provider_secret(SimpleNamespace(credential_secret="")) == ""
    assert provider_secret(SimpleNamespace(credential_secret=None)) == ""
    assert provider_secret(SimpleNamespace()) == ""


def test_sealed_credential_without_key_fails_closed(monkeypatch, secrets_key):
    provider = SimpleNamespace(credential_secret=sealed_secret("sk-abc123"))
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "", raising=False)
    secretbox._reset_cache()
    with pytest.raises(SecretBoxError):
        provider_secret(provider)


def test_sealed_secret_without_key_returns_plaintext():
    secretbox._reset_cache()
    assert sealed_secret("sk-plain") == "sk-plain"
    assert sealed_secret("") == ""


# ── Write sites (service + startup pass) — in-memory LLMProvider fake ────────
# asyncio_mode = "auto" (pyproject) picks the async tests up without markers.


class _Node:
    def __init__(self, **kw):
        defaults = dict(
            org_uid="", label="p", kind="mlx", base_url="", model="", api_key_env="",
            cli_command_template="", extra_args="", enabled=True, active=False,
            fallback_priority=100, notes="", credential_secret="",
            last_health_check_at=None, last_health_status="unknown",
            last_health_detail="", created_at=None, updated_at=None,
        )
        defaults.update(kw)
        self.__dict__.update(defaults)

    async def save(self):
        if self not in _STORE:
            _STORE.append(self)
        return self

    async def delete(self):
        _STORE.remove(self)


_STORE: list[_Node] = []


class _Nodes:
    async def all(self):
        return list(_STORE)

    async def filter(self, **kw):
        """Filter by keyword arguments — supports org_uid."""
        return [n for n in _STORE if all(getattr(n, k, None) == v for k, v in kw.items())]

    async def get_or_none(self, **kw):
        for n in _STORE:
            if all(getattr(n, k, None) == v for k, v in kw.items()):
                return n
        return None


class FakeLLMProvider(_Node):
    nodes = _Nodes()


@pytest.fixture
def fake_providers(monkeypatch, secrets_key):
    _STORE.clear()
    monkeypatch.setattr(svc_mod, "LLMProvider", FakeLLMProvider)
    monkeypatch.setattr(credentials_mod, "LLMProvider", FakeLLMProvider)

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(svc_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _user(org="org-a"):
    return UserDTO(
        uid="u1", email="a@b.c", display_name="A", role="admin",
        org_uid=org, org_role="owner", is_platform_admin=False,
    )


async def test_create_stores_sealed_credential(fake_providers):
    req = CreateLLMProviderRequest(
        label="Acme Claude", kind=LLMProviderKind.CLAUDE_API, credential_secret="sk-acme"
    )
    dto = await LLMProviderService().create(req, user=_user())
    node = await FakeLLMProvider.nodes.get_or_none(uid=dto.uid)
    # Stored sealed, readable back through the read helper.
    assert secretbox.is_sealed(node.credential_secret)
    assert provider_secret(node) == "sk-acme"
    # DTO signals presence but NEVER carries the secret (in any form).
    assert dto.has_credential_secret is True
    assert "sk-acme" not in dto.model_dump_json()


async def test_create_without_credential_stores_empty(fake_providers):
    req = CreateLLMProviderRequest(label="No cred", kind=LLMProviderKind.MLX)
    dto = await LLMProviderService().create(req, user=_user())
    node = await FakeLLMProvider.nodes.get_or_none(uid=dto.uid)
    assert node.credential_secret == ""
    assert dto.has_credential_secret is False


async def test_update_replaces_credential_with_sealed_value(fake_providers):
    n = FakeLLMProvider(uid="p1", org_uid="org-a", credential_secret=sealed_secret("sk-old"))
    _STORE.append(n)
    dto = await LLMProviderService().update(
        "p1", UpdateLLMProviderRequest(credential_secret="sk-new"), user=_user()
    )
    assert secretbox.is_sealed(n.credential_secret)
    assert provider_secret(n) == "sk-new"
    assert dto.has_credential_secret is True
    assert "sk-new" not in dto.model_dump_json()


async def test_update_can_clear_credential(fake_providers):
    n = FakeLLMProvider(uid="p1", org_uid="org-a", credential_secret=sealed_secret("sk-old"))
    _STORE.append(n)
    dto = await LLMProviderService().update(
        "p1", UpdateLLMProviderRequest(credential_secret=""), user=_user()
    )
    assert n.credential_secret == ""
    assert dto.has_credential_secret is False


async def test_encrypt_plaintext_provider_secrets_seals_legacy_rows(fake_providers):
    legacy = FakeLLMProvider(uid="legacy", org_uid="org-a", credential_secret="sk-plain")
    already = FakeLLMProvider(
        uid="sealed", org_uid="org-a", credential_secret=sealed_secret("sk-done")
    )
    empty = FakeLLMProvider(uid="empty", org_uid="org-a", credential_secret="")
    _STORE.extend([legacy, already, empty])

    changed = await encrypt_plaintext_provider_secrets()

    assert changed == 1
    assert secretbox.is_sealed(legacy.credential_secret)
    assert provider_secret(legacy) == "sk-plain"
    # Idempotent: a second pass changes nothing.
    assert await encrypt_plaintext_provider_secrets() == 0
    assert provider_secret(already) == "sk-done"
    assert empty.credential_secret == ""


async def test_one_undecryptable_row_does_not_abort_the_pass(fake_providers, monkeypatch):
    # A row sealed under a key absent from primary+fallbacks must be skipped
    # (logged), not abort the pass — later plaintext rows still get sealed.
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", "some-other-key-entirely-123", raising=False)
    secretbox._reset_cache()
    wedged = FakeLLMProvider(uid="wedged", org_uid="org-a", credential_secret=sealed_secret("sk-x"))
    monkeypatch.setattr(settings, "OPENSWEEP_SECRETS_KEY", KEY, raising=False)
    secretbox._reset_cache()
    plain = FakeLLMProvider(uid="plain", org_uid="org-a", credential_secret="sk-plain")
    _STORE.extend([wedged, plain])

    changed = await encrypt_plaintext_provider_secrets()

    assert changed == 1
    assert secretbox.is_sealed(plain.credential_secret)
    assert provider_secret(plain) == "sk-plain"
    # The wedged row is untouched (still sealed under the lost key).
    with pytest.raises(secretbox.SecretBoxError):
        provider_secret(wedged)
