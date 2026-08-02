"""LLMProvider credential seal/unseal helpers (infrastructure/secretbox.py).

Every READ of `credential_secret` goes through provider_secret() so sealed
(enc:v1:) and legacy plaintext rows both resolve to the usable secret.
SecretBoxError deliberately propagates at read sites: a run must fail loudly
rather than run credential-less against a provider.
"""

from __future__ import annotations

import logging

from domains.llm_providers.models import LLMProvider
from infrastructure import secretbox

logger = logging.getLogger(__name__)


def provider_secret(provider) -> str:
    """The plaintext credential for a provider node (or stub). Raises
    SecretBoxError when a sealed value cannot be decrypted — fail closed."""
    return secretbox.unseal((getattr(provider, "credential_secret", "") or "").strip())


def sealed_secret(plaintext: str) -> str:
    """Seal a plaintext credential for storage ('' stays '')."""
    return secretbox.seal(plaintext or "")


async def reseal_sealed_field(model, field: str) -> int:
    """Idempotent pass over one node label: seal plaintext values in `field`
    and re-seal rows still under a fallback key. Returns the number changed.

    Generic twin of encrypt_plaintext_provider_secrets — used at startup for
    GitConnection.token_sealed and SlackConnection.bot_token_sealed, which the
    provider-only pass left plaintext forever if they were written while
    OPENSWEEP_SECRETS_KEY was unset.
    """
    if not secretbox.configured():
        return 0
    changed = 0
    for node in await model.nodes.all():
        current = (getattr(node, field, "") or "").strip()
        if not current:
            continue
        try:
            updated = (
                secretbox.rotate(current)
                if secretbox.is_sealed(current)
                else secretbox.seal(current)
            )
        except secretbox.SecretBoxError as exc:
            logger.warning(
                f"credential sealing skipped for {model.__name__} "
                f"{getattr(node, 'uid', '?')}: {exc}",
                extra={"tag": "secrets"},
            )
            continue
        if updated != current:
            setattr(node, field, updated)
            await node.save()
            changed += 1
    return changed


async def encrypt_plaintext_provider_secrets() -> int:
    """One idempotent pass over all LLMProviders: seal plaintext credentials,
    re-seal rows still under a fallback key. Returns the number changed."""
    if not secretbox.configured():
        return 0
    changed = 0
    # Deliberate all-orgs pass: key rotation re-seals EVERY provider's secret,
    # so this is not org-scoped (unlike llm_provider_service reads). Do not
    # convert to a per-org filter.
    for provider in await LLMProvider.nodes.all():
        current = (provider.credential_secret or "").strip()
        if not current:
            continue
        try:
            updated = (
                secretbox.rotate(current)
                if secretbox.is_sealed(current)
                else secretbox.seal(current)
            )
        except secretbox.SecretBoxError as exc:
            # One undecryptable row (e.g. sealed under a key missing from
            # OPENSWEEP_SECRETS_KEY_FALLBACKS) must not abort the pass — the
            # remaining plaintext rows still need sealing.
            logger.warning(
                f"credential sealing skipped for provider {provider.uid}: {exc}",
                extra={"tag": "secrets"},
            )
            continue
        if updated != current:
            provider.credential_secret = updated
            await provider.save()
            changed += 1
    return changed
