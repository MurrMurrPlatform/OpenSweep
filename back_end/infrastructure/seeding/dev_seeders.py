"""Dev-only seeders: the local User and a baseline set of LLMProviders.

These are the "dev" group in the registry — they are NEVER run at backend
startup (a boot must not touch provider credentials or invent users). They
exist for the migration_tool / a fresh local graph, invoked explicitly.

Credentials come from the environment only. There is deliberately no
committed fallback token: set CLAUDE_CODE_OAUTH_TOKEN in your shell / .env to
have the claude-subscription provider seeded with a working credential;
otherwise it is seeded credential-less and you fill it in through the UI.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from domains.llm_providers.models import LLMProvider
from domains.llm_providers.services.credentials import sealed_secret
from infrastructure.seeding.base import SeedMode, SeedResult
from logging_config import logger


async def seed_local_user(mode: SeedMode = SeedMode.UPSERT) -> SeedResult:
    """The single local User node (LOCAL_USER_UID). Idempotent."""
    from domains.organizations.models import LOCAL_ORG_UID
    from domains.users.models import User
    from domains.users.services.local_user import LOCAL_USER_UID

    res = SeedResult(name="local_user")
    email = os.environ.get("OPENSWEEP_LOCAL_USER_EMAIL", "local@opensweep.dev")
    name = os.environ.get("OPENSWEEP_LOCAL_USER_DISPLAY_NAME", "Local User")

    existing = await User.nodes.get_or_none(uid=LOCAL_USER_UID)
    if existing is not None:
        changed = existing.email != email or existing.display_name != name
        if changed:
            existing.email = email
            existing.display_name = name
            await existing.save()
            res.updated = 1
        else:
            res.unchanged = 1
        return res

    u = User(
        uid=LOCAL_USER_UID,
        email=email,
        display_name=name,
        org_uid=LOCAL_ORG_UID,
    )
    await u.save()
    res.created = 1
    return res


def _claude_oauth_token() -> str:
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")


def _baseline_providers() -> list[dict[str, Any]]:
    return [
        {
            "slug": "claude-subscription",
            "label": "Claude Code (subscription)",
            "kind": "claude_subscription",
            "model": "claude-sonnet-4-6",
            # Order matters: claude's `-p` consumes its prompt as the IMMEDIATELY
            # following positional. Putting other flags between -p and the prompt
            # leaves the prompt to be picked up by --mcp-config (which keeps
            # accepting paths until the next flag), producing a confusing
            # ENAMETOOLONG when the prompt is treated as a config file path.
            "cli_command_template": (
                "claude -p {{instruction_q}} "
                "--output-format stream-json --verbose "
                "--permission-mode bypassPermissions "
                "--mcp-config {{mcp_config_path_q}}"
            ),
            "credential_secret": _claude_oauth_token(),
            "enabled": True,
            "active": True,
        },
        {
            "slug": "omlx-local-opencode",
            "label": "OMLX Local OpenCode",
            # Earlier label this row may have been seeded under; the seeder picks
            # up any matching alias and renames the row to `label` in place.
            "aliases": ["MLX (local)"],
            "kind": "opencode",
            "base_url": "http://host.docker.internal:2345/v1",
            # `opensweep/` matches the auto-generated opencode provider name written
            # into opencode.json by llm_executor._prepare_opencode_config; the
            # suffix is the model id the OMLX server exposes.
            "model": "opensweep/Qwen3.6-35B-A3B-4bit",
            # {{model_q}} (shlex-quoted) keeps the model slug one argv token.
            "cli_command_template": "opencode run -m {{model_q}} {{instruction_q}}",
            "enabled": True,
            "active": False,
        },
        {
            "slug": "claude-api",
            "label": "Anthropic API",
            "kind": "claude_api",
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "enabled": False,
            "active": False,
        },
    ]


async def _deactivate_others(keep_uid: str) -> None:
    from domains.organizations.models import LOCAL_ORG_UID

    # Providers are strictly org-owned — the dev seeder only ever manages the
    # local org's rows, so other orgs' active choices are never touched.
    for other in await LLMProvider.nodes.all():
        if (
            other.uid != keep_uid
            and (getattr(other, "org_uid", "") or "") == LOCAL_ORG_UID
            and bool(getattr(other, "active", False))
        ):
            other.active = False
            await other.save()


async def seed_llm_providers(mode: SeedMode = SeedMode.UPSERT) -> SeedResult:
    """Baseline LLMProviders. Idempotent and credential-preserving: an
    existing row's credential is never cleared, and the user's active choice
    is only changed when a baseline row asks to become active and none is.

    The `cli_command_template` is platform-owned (we ship corrections to it),
    so it is refreshed on SYNC/FORCE; under UPSERT existing rows are left as-is.
    """
    from domains.organizations.models import LOCAL_ORG_UID

    res = SeedResult(name="llm_providers")
    all_rows = await LLMProvider.nodes.all()
    existing = {p.label: p for p in all_rows}
    # Respect the operator's active choice: only auto-activate a baseline
    # provider when NOTHING is active in the local org (fresh install, or
    # everything disabled). Never flip the active row out from under a
    # re-seed — and never look at other orgs' rows.
    active_exists = any(
        bool(getattr(p, "active", False))
        for p in all_rows
        if (getattr(p, "org_uid", "") or "") == LOCAL_ORG_UID
    )

    for spec in _baseline_providers():
        secret = spec.get("credential_secret") or ""
        matched_label = next(
            (lbl for lbl in (spec["label"], *spec.get("aliases", [])) if lbl in existing),
            None,
        )

        if matched_label is not None:
            p = existing[matched_label]
            touched = False
            # Legacy-unowned rows (pre-tenancy) are adopted by the local org.
            if not (p.org_uid or ""):
                p.org_uid = LOCAL_ORG_UID
                touched = True
            # Rename in place when matched via an alias (row repurposed).
            renamed = p.label != spec["label"]
            if renamed:
                p.label = spec["label"]
                touched = True
            # Refresh the credential only when we have one and the row lacks it.
            # Sealed at rest (infrastructure/secretbox.py).
            if secret and not (p.credential_secret or "").strip():
                p.credential_secret = sealed_secret(secret)
                touched = True
            # Honor a baseline "active" request only if nothing else is active.
            if (
                bool(spec.get("active", False))
                and not bool(getattr(p, "active", False))
                and not active_exists
            ):
                await _deactivate_others(p.uid)
                p.active = True
                p.enabled = True
                active_exists = True
                touched = True
            # cli_command_template is platform-owned; sync it on SYNC/FORCE.
            spec_tmpl = (spec.get("cli_command_template") or "").strip()
            if (
                mode is not SeedMode.UPSERT
                and spec_tmpl
                and (p.cli_command_template or "").strip() != spec_tmpl
            ):
                p.cli_command_template = spec_tmpl
                touched = True
            # A repurposed row (alias rename) must adopt the new transport shape.
            if renamed:
                for field in ("kind", "base_url", "model"):
                    val = spec.get(field)
                    if val is not None and getattr(p, field, None) != val:
                        setattr(p, field, val)
                        touched = True
            if touched:
                await p.save()
                res.updated += 1
            else:
                res.unchanged += 1
            continue

        want_active = bool(spec.get("active", False)) and not active_exists
        p = LLMProvider(
            uid=uuid4().hex,
            org_uid=LOCAL_ORG_UID,
            label=spec["label"],
            kind=spec["kind"],
            base_url=spec.get("base_url", ""),
            model=spec.get("model", ""),
            api_key_env=spec.get("api_key_env", ""),
            cli_command_template=spec.get("cli_command_template", ""),
            credential_secret=sealed_secret(secret),
            enabled=bool(spec.get("enabled", True)),
            active=want_active,
        )
        if want_active:
            await _deactivate_others(p.uid)
            active_exists = True
        await p.save()
        res.created += 1

    if res.created or res.updated:
        logger.info(
            f"LLM providers: +{res.created} created, {res.updated} updated",
            extra={"tag": "seeding"},
        )
    return res
