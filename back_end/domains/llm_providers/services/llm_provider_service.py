"""LLMProvider CRUD + simple health check.

Tenancy: providers are STRICTLY org-owned — org_uid is always the owning
org's uid, and there is no shared/platform scope. An org's callers only ever
see — and runs only ever select from — their own org's providers; the active
flag and the §8 fallback chain operate within that single org. Legacy rows
with org_uid == "" are unowned data: invisible, unselectable, unmanageable
(migration m0003 stamps them to the local org at startup).
"""

import os
import shutil
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from neomodel import adb

from domains.llm_providers.models import LLMProvider
from domains.llm_providers.schemas import (
    CreateLLMProviderRequest,
    LLMProviderDTO,
    LLMProviderHealth,
    LLMProviderKind,
    UpdateLLMProviderRequest,
    default_cli_template,
    kind_meta,
)
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

from .credentials import sealed_secret


def _scope(n) -> str:
    """Provider tenancy scope — "" only for legacy-unowned (unusable) rows."""
    return getattr(n, "org_uid", "") or ""


async def repository_org_uid(repository_uid: str) -> str:
    """The org that owns a repository — how run dispatch resolves which
    providers it may use."""
    rows, _ = await adb.cypher_query(
        "MATCH (r:Repository {uid: $uid}) RETURN r.org_uid", {"uid": repository_uid}
    )
    return (rows[0][0] or "") if rows else ""


async def visible_providers(org_uid: str) -> list[LLMProvider]:
    """The org's own providers, nothing else. A caller without an org sees
    none — legacy-unowned rows (org_uid == "") are invisible to everyone."""
    if not org_uid:
        return []
    return list(await LLMProvider.nodes.filter(org_uid=org_uid))


def _can_manage(n, user: UserDTO) -> bool:
    """A provider is manageable iff it belongs to the caller's org
    (route-level require_role('admin') supplies the role gate)."""
    return bool(user.org_uid) and _scope(n) == user.org_uid


def _require_manageable(n, user: UserDTO) -> None:
    if not _can_manage(n, user):
        # Another org's (or unowned) provider — its existence never leaks.
        raise HTTPException(status_code=404, detail="LLMProvider not found")


class LLMProviderService:
    async def list_providers(self, org_uid: str) -> list[LLMProviderDTO]:
        return [_to_dto(n) for n in await visible_providers(org_uid)]

    async def get_active(self, org_uid: str) -> LLMProvider:
        active = await get_active_provider(org_uid)
        if active is None:
            raise HTTPException(
                status_code=409,
                detail="No LLM provider configured for your organization. Add one and mark it active in Settings → LLM Providers, then run its health check.",
            )
        return active

    async def status(self, org_uid: str) -> dict:
        """Lightweight onboarding probe — 200 always: a fresh org with no
        providers yet is a normal steady state, not an error."""
        providers = await visible_providers(org_uid)
        active = await get_active_provider(org_uid)
        return {
            "configured": active is not None,
            "provider_count": len(providers),
            "active_uid": (active.uid or "") if active is not None else "",
            "active_label": (active.label or "") if active is not None else "",
        }

    async def get(self, uid: str, org_uid: str) -> LLMProviderDTO:
        n = await LLMProvider.nodes.get_or_none(uid=uid)
        if n is None or _scope(n) != org_uid:
            raise HTTPException(status_code=404, detail=f"LLMProvider {uid} not found")
        return _to_dto(n)

    async def create(self, req: CreateLLMProviderRequest, *, user: UserDTO) -> LLMProviderDTO:
        scope = user.org_uid
        if not scope:
            raise HTTPException(
                status_code=422,
                detail="user has no organization — cannot create an LLM provider",
            )
        active = bool(req.active)
        if not active:
            active = await _scope_active(scope) is None
        # Everything but the kind is platform-defaultable — the connect
        # dialog sends as little as {kind, credential_secret} and the row
        # still comes out fully dispatchable.
        meta = kind_meta(req.kind)
        n = LLMProvider(
            uid=uuid4().hex,
            org_uid=scope,
            label=req.label.strip() or str(meta.get("default_label") or req.kind.value),
            kind=req.kind.value,
            base_url=req.base_url.strip() or str(meta.get("default_base_url") or ""),
            model=req.model.strip() or str(meta.get("default_model") or ""),
            api_key_env=req.api_key_env.strip() or str(meta.get("default_api_key_env") or ""),
            cli_command_template=req.cli_command_template.strip()
            or default_cli_template(req.kind),
            extra_args=req.extra_args,
            enabled=True if active else req.enabled,
            active=active,
            fallback_priority=int(req.fallback_priority),
            notes=req.notes,
            credential_secret=sealed_secret(req.credential_secret or ""),
        )
        if active:
            await _deactivate_others(n.uid, scope)
        await n.save()
        await write_audit(
            kind="llm_provider.created", subject_uid=n.uid, subject_type="LLMProvider",
            actor_uid=user.uid, payload={"label": n.label, "kind": n.kind},
        )
        return _to_dto(n)

    async def update(self, uid: str, req: UpdateLLMProviderRequest, *, user: UserDTO) -> LLMProviderDTO:
        n = await LLMProvider.nodes.get_or_none(uid=uid)
        if n is None:
            raise HTTPException(status_code=404, detail=f"LLMProvider {uid} not found")
        _require_manageable(n, user)
        scope = _scope(n)
        data = req.model_dump(exclude_unset=True)
        make_active = bool(data.get("active"))
        for field, value in data.items():
            if hasattr(value, "value"):
                value = value.value
            if field == "credential_secret":
                # Credentials are sealed at rest (infrastructure/secretbox.py).
                value = sealed_secret(value or "")
                # A re-paste clears any dead/uncertain state and bumps the
                # credential revision, so a codex write-back still in flight
                # from a prior turn loses its compare-and-swap and cannot
                # clobber the credential the user just supplied.
                n.needs_reauth = False
                n.auth_state_uncertain = False
                n.credential_revision = int(getattr(n, "credential_revision", 0) or 0) + 1
            setattr(n, field, value)
        if not (n.cli_command_template or "").strip():
            # Clearing the template (or switching kind without one) means
            # "reset to the platform default for this kind".
            n.cli_command_template = default_cli_template(n.kind)
        if make_active:
            n.enabled = True
            await _deactivate_others(n.uid, scope)
        n.updated_at = datetime.now(UTC)
        await n.save()
        await _ensure_one_active(scope)
        await write_audit(
            kind="llm_provider.updated", subject_uid=n.uid, subject_type="LLMProvider",
            actor_uid=user.uid, payload={"changed": list(data.keys())},
        )
        return _to_dto(n)

    async def delete(self, uid: str, *, user: UserDTO) -> None:
        n = await LLMProvider.nodes.get_or_none(uid=uid)
        if n is None:
            raise HTTPException(status_code=404, detail=f"LLMProvider {uid} not found")
        _require_manageable(n, user)
        scope = _scope(n)
        await n.delete()
        await _ensure_one_active(scope)
        await write_audit(
            kind="llm_provider.deleted", subject_uid=uid, subject_type="LLMProvider",
            actor_uid=user.uid,
        )

    async def check_health(self, uid: str, *, user: UserDTO) -> LLMProviderDTO:
        n = await LLMProvider.nodes.get_or_none(uid=uid)
        if n is None or _scope(n) != user.org_uid:
            raise HTTPException(status_code=404, detail=f"LLMProvider {uid} not found")
        status, detail = await _probe(n)
        n.last_health_check_at = datetime.now(UTC)
        n.last_health_status = status.value
        n.last_health_detail = detail[:300]
        await n.save()
        return _to_dto(n)


async def _probe(n: LLMProvider) -> tuple[LLMProviderHealth, str]:
    """Best-effort connectivity probe — never raises, never blocks the loop."""
    kind = n.kind
    try:
        if kind in (LLMProviderKind.CLAUDE_SUBSCRIPTION.value, LLMProviderKind.CODEX_SUBSCRIPTION.value):
            binary = "claude" if kind == LLMProviderKind.CLAUDE_SUBSCRIPTION.value else "codex"
            path = shutil.which(binary)
            if not path:
                return LLMProviderHealth.UNREACHABLE, f"{binary} CLI not on PATH"
            return LLMProviderHealth.OK, f"{binary} found at {path}"
        if kind == LLMProviderKind.CLAUDE_API.value or kind == LLMProviderKind.OPENAI_API.value:
            if (n.credential_secret or "").strip():
                return LLMProviderHealth.OK, "credential secret present"
            env = n.api_key_env or ("ANTHROPIC_API_KEY" if kind == LLMProviderKind.CLAUDE_API.value else "OPENAI_API_KEY")
            if not os.environ.get(env):
                return LLMProviderHealth.UNREACHABLE, f"env {env} not set"
            return LLMProviderHealth.OK, f"env {env} present"
        if kind in (LLMProviderKind.MLX.value, LLMProviderKind.LMSTUDIO.value, LLMProviderKind.OLLAMA.value):
            if not n.base_url:
                return LLMProviderHealth.UNREACHABLE, "base_url is empty"
            # OpenAI-compatible servers expose /models; Ollama exposes /api/tags natively
            # but also /v1/models when called with the OpenAI shim path.
            probe_url = n.base_url.rstrip("/") + "/models"
            import httpx
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(probe_url)
                    # 4xx/5xx count as unreachable (urllib parity).
                    resp.raise_for_status()
                    return LLMProviderHealth.OK, f"{probe_url} -> {resp.status_code}"
            except Exception as exc:
                return LLMProviderHealth.UNREACHABLE, f"{probe_url}: {str(exc)[:180]}"
        return LLMProviderHealth.UNKNOWN, "no probe configured for this kind"
    except Exception as exc:  # safety net
        return LLMProviderHealth.UNKNOWN, str(exc)[:200]


def _to_dto(n: LLMProvider) -> LLMProviderDTO:
    return LLMProviderDTO(
        uid=n.uid,
        org_uid=_scope(n),
        label=n.label,
        kind=n.kind,
        base_url=n.base_url or "",
        model=n.model or "",
        api_key_env=n.api_key_env or "",
        cli_command_template=n.cli_command_template or "",
        extra_args=n.extra_args or "",
        enabled=bool(n.enabled),
        active=bool(getattr(n, "active", False)),
        fallback_priority=int(getattr(n, "fallback_priority", None) or 100),
        notes=n.notes or "",
        has_credential_secret=bool((n.credential_secret or "").strip()),
        needs_reauth=bool(getattr(n, "needs_reauth", False)),
        auth_state_uncertain=bool(getattr(n, "auth_state_uncertain", False)),
        last_health_check_at=n.last_health_check_at,
        last_health_status=n.last_health_status or "unknown",
        last_health_detail=n.last_health_detail or "",
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


def provider_to_dto(n: LLMProvider) -> LLMProviderDTO:
    return _to_dto(n)


async def _scope_active(scope: str) -> LLMProvider | None:
    """The usable active provider WITHIN one org scope, healing old data
    with multiple actives in that scope."""
    if not scope:
        return None
    nodes = list(await LLMProvider.nodes.filter(org_uid=scope))
    enabled = [p for p in nodes if bool(getattr(p, "enabled", True))]
    active = [p for p in enabled if bool(getattr(p, "active", False))]
    if not active:
        return None
    winner = active[0]
    for p in active[1:]:
        p.active = False
        await p.save()
    if (winner.last_health_status or "unknown") == LLMProviderHealth.UNREACHABLE.value:
        return None
    return winner


async def get_active_provider(org_uid: str) -> LLMProvider | None:
    """The org's active provider — no fallback outside the org."""
    if not org_uid:
        return None
    return await _scope_active(org_uid)


def choose_provider(providers, exclude_uids: set[str] | frozenset[str] = frozenset()):
    """Pure fallback-chain selection (PLATFORM_V2_DESIGN.md §8).

    Order: the first usable active provider in list order; if none, the next
    healthy enabled provider by ascending `fallback_priority`, ties broken by
    label then uid.

    Operates on any objects exposing the LLMProvider attributes so the
    ordering + exclusion rules stay unit-testable without Neo4j.
    """

    def usable(p) -> bool:
        if not bool(getattr(p, "enabled", True)):
            return False
        health = (getattr(p, "last_health_status", None) or "unknown")
        return health != LLMProviderHealth.UNREACHABLE.value

    for p in providers:
        if bool(getattr(p, "active", False)) and usable(p) and p.uid not in exclude_uids:
            return p
    fallbacks = [
        p
        for p in providers
        if usable(p) and p.uid not in exclude_uids and not bool(getattr(p, "active", False))
    ]
    fallbacks.sort(
        key=lambda p: (
            int(getattr(p, "fallback_priority", None) or 100),
            getattr(p, "label", "") or "",
            p.uid,
        )
    )
    return fallbacks[0] if fallbacks else None


async def select_provider(
    *, org_uid: str, exclude_uids: set[str] | frozenset[str] = frozenset()
) -> LLMProvider | None:
    """Active provider first; else the next provider in the fallback chain —
    restricted to the org's own providers.

    `exclude_uids` carries the quota-exhausted providers recorded on a paused
    run, so a retry lands on the next provider in the chain.
    """
    nodes = await visible_providers(org_uid)
    return choose_provider(nodes, exclude_uids=set(exclude_uids))


async def _deactivate_others(active_uid: str, scope: str) -> None:
    for p in await LLMProvider.nodes.filter(org_uid=scope):
        if p.uid != active_uid and bool(getattr(p, "active", False)):
            p.active = False
            await p.save()


async def _ensure_one_active(scope: str) -> None:
    """Keep one active provider within the org scope while it has enabled
    ones — a scope going empty is fine (runs then fail with a clear
    no-provider message). Promote along the fallback chain
    (fallback_priority, label, uid), not store order."""
    if await _scope_active(scope) is not None:
        return
    nodes = list(await LLMProvider.nodes.filter(org_uid=scope))
    candidate = choose_provider(nodes)
    if candidate is None:
        # Every enabled provider in scope is unreachable — still promote by the
        # same ordering so a scope never sits with nothing active.
        enabled = [p for p in nodes if bool(getattr(p, "enabled", True))]
        enabled.sort(
            key=lambda p: (
                int(getattr(p, "fallback_priority", None) or 100),
                getattr(p, "label", "") or "",
                p.uid,
            )
        )
        candidate = enabled[0] if enabled else None
    if candidate is None:
        return
    candidate.active = True
    await candidate.save()
