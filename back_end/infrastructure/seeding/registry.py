"""The seeder registry — the single source of truth for what a fresh OpenSweep
needs seeded, and the one entry point every caller uses.

Backend startup, dev_reset, and the migration_tool all call `run_seeders`, so
the set of seeds can never drift between them (the bug this replaced: variant
prompts and per-repo Investigations were seeded by some paths and not others).

Two groups:
  platform — run everywhere (startup, dev_reset, tool). The rows a running
             OpenSweep needs regardless of who is operating it.
  dev      — the local User and baseline LLMProviders. NEVER run at startup
             (a boot must not touch credentials); the migration_tool opts in.

Every seeder is `async (SeedMode) -> SeedResult` and must be idempotent. Most
are upsert-only by nature and ignore the mode; the prompt seeders and the
provider template honor SYNC/FORCE (see SeedMode).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Iterable

from logging_config import logger

from infrastructure.seeding.base import SeedMode, SeedResult
from infrastructure.seeding.lock import seed_lock

PLATFORM = "platform"
DEV = "dev"


@dataclass(frozen=True)
class Seeder:
    name: str
    group: str
    run: Callable[[SeedMode], Awaitable[SeedResult]]


# ── Individual seeders ──────────────────────────────────────────────────────


async def _seed_system_run_policy(mode: SeedMode) -> SeedResult:
    from domains.run_policies.services.system_default import (
        ensure_system_default,
        get_system_default,
    )

    res = SeedResult(name="system_run_policy")
    existed = await get_system_default() is not None
    await ensure_system_default()  # upsert-only; preserves human tuning by design
    if existed:
        res.unchanged = 1
    else:
        res.created = 1
    return res


async def _seed_ecc_prompts(mode: SeedMode) -> SeedResult:
    """Bootstrap the prompt library from ECC — only when it is empty. Network-
    and git-dependent, so failures are recorded, never raised."""
    from domains.agents.models import Agent
    from domains.agents.services.ecc_import import import_ecc

    res = SeedResult(name="ecc_prompts")
    if await Agent.nodes.filter(provenance="imported"):
        res.note = "library not empty — skipped"
        return res
    try:
        result = await import_ecc()
        res.created = result.imported
        res.note = f"commit={result.source_commit[:8]}"
    except Exception as exc:  # noqa: BLE001 — external clone; never fatal
        res.error = str(exc)
        logger.warning(f"ECC import skipped: {exc}", extra={"tag": "seeding"})
    return res


async def _seed_workflow_default_prompts(mode: SeedMode) -> SeedResult:
    from domains.agents.services.seed_defaults import seed_workflow_default_prompts

    return await seed_workflow_default_prompts(mode)


async def _seed_variant_prompts(mode: SeedMode) -> SeedResult:
    from domains.agents.services.seed_variants import seed_variant_prompts

    return await seed_variant_prompts(mode)


async def _seed_agent_base_prompts(mode: SeedMode) -> SeedResult:
    from domains.agents.services.seed_agent_bases import seed_agent_base_prompts

    return await seed_agent_base_prompts(mode)


async def _seed_per_repo(mode: SeedMode) -> SeedResult:
    """Per repository: one pinned conventions Doc page and the on-event
    "keep docs current" / "audit stale" ScheduledAgent bindings. All
    idempotent."""
    from domains.agents.services.scheduled_agent_service import (
        seed_audit_agents,
        seed_audit_stale,
        seed_keep_docs_current,
    )
    from domains.docs.services.doc_service import seed_conventions_doc
    from domains.repositories.models import Repository

    res = SeedResult(name="per_repo")
    conventions = keep_docs = audit_stale = audit_agents = 0
    for repo in await Repository.nodes.all():
        if await seed_conventions_doc(repo.uid) is not None:
            conventions += 1
        if await seed_keep_docs_current(repo.uid) is not None:
            keep_docs += 1
        if await seed_audit_stale(repo.uid) is not None:
            audit_stale += 1
        audit_agents += len(await seed_audit_agents(repo.uid))
    res.created = conventions + keep_docs + audit_stale + audit_agents
    res.note = (
        f"conventions={conventions} keep_docs={keep_docs} "
        f"audit_stale={audit_stale} audit_agents={audit_agents}"
    )
    return res


async def _seed_local_user(mode: SeedMode) -> SeedResult:
    from infrastructure.seeding.dev_seeders import seed_local_user

    return await seed_local_user(mode)


async def _seed_llm_providers(mode: SeedMode) -> SeedResult:
    from infrastructure.seeding.dev_seeders import seed_llm_providers

    return await seed_llm_providers(mode)


# Order matters: policy before prompts before per-repo (per-repo Investigations
# reference stages whose default prompts should already exist). Dev seeders last.
SEEDERS: list[Seeder] = [
    Seeder("system_run_policy", PLATFORM, _seed_system_run_policy),
    Seeder("ecc_prompts", PLATFORM, _seed_ecc_prompts),
    Seeder("workflow_default_prompts", PLATFORM, _seed_workflow_default_prompts),
    Seeder("variant_prompts", PLATFORM, _seed_variant_prompts),
    Seeder("agent_base_prompts", PLATFORM, _seed_agent_base_prompts),
    Seeder("per_repo", PLATFORM, _seed_per_repo),
    Seeder("local_user", DEV, _seed_local_user),
    Seeder("llm_providers", DEV, _seed_llm_providers),
]


# ── Entry point ─────────────────────────────────────────────────────────────


def select_seeders(
    groups: Iterable[str], names: Iterable[str] | None = None
) -> list[Seeder]:
    """The seeders to run for a group/name filter, in registry order. Pure —
    unit-tested without a database."""
    wanted = set(groups)
    only = set(names) if names is not None else None
    return [
        s for s in SEEDERS if s.group in wanted and (only is None or s.name in only)
    ]


async def run_seeders(
    mode: SeedMode = SeedMode.SYNC,
    *,
    groups: Iterable[str] = (PLATFORM,),
    names: Iterable[str] | None = None,
    lock: bool = True,
    lock_timeout_seconds: int = 120,
) -> dict[str, SeedResult]:
    """Run the seeders for the given groups, in registry order, under the seed
    lease-lock (so replicas / a racing tool can't double-create). One seeder
    failing is recorded in its SeedResult and does not stop the others — the
    same best-effort posture the startup seeders had, now uniform.

    `names`, when given, restricts the run to those seeder names (still ordered
    by the registry) — the migration_tool uses it for single-bundle seeds.
    """
    selected = select_seeders(groups, names)

    async def _run_all() -> dict[str, SeedResult]:
        out: dict[str, SeedResult] = {}
        for s in selected:
            try:
                out[s.name] = await s.run(mode)
            except Exception as exc:  # noqa: BLE001 — one seeder must not abort the pass
                out[s.name] = SeedResult(name=s.name, error=str(exc))
                logger.warning(f"seeder {s.name} failed: {exc}", extra={"tag": "seeding"})
        return out

    if lock:
        async with seed_lock(timeout_seconds=lock_timeout_seconds):
            results = await _run_all()
    else:
        results = await _run_all()

    summary = {n: r.as_dict() for n, r in results.items()}
    logger.info(f"seeding done (mode={mode.value}): {summary}", extra={"tag": "seeding"})
    return results


def summarize(results: dict[str, SeedResult]) -> dict[str, dict]:
    """JSON-serializable view of run_seeders output (for CLI/HTTP responses)."""
    return {name: res.as_dict() for name, res in results.items()}
