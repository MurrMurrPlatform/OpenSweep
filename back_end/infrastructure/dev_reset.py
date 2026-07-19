"""Full dev reset — wipe derived state, then seed everything a fresh
install has. THE canonical reset path: the admin API endpoint
(POST /api/v1/platform-config/dev-reset), scripts/reseed.py, and any UI
tooling must all go through `dev_reset()` so seeding can never be skipped.

Wipes: Doc, DocEdit, Memory, Checked, Finding, Run, ScheduledAgent, plus
delivery ledger state bound to them (PullRequest, FindingResolution,
Verdict, MergePolicy? — no: MergePolicy is config, kept) and orphaned
legacy Knowledge/CoverageRecord/MapNode/MapEdge nodes via Cypher.

Keeps (configuration): Repository, Organization, User, LLMProvider,
PlatformConfig, RunPolicy, AgentPrompt, MergePolicy, Ticket comments? —
Tickets and Comments are work-tracking state and ARE wiped (they reference
wiped findings/runs). Kept repositories missing org_uid are stamped into
the local org (tenancy cutover).

Seeds: system default RunPolicy, ECC prompt library (only when the library
is empty), the "OpenSweep default — <Stage>" workflow prompts, and — per
repository — one pinned empty conventions Doc page plus the on-event
"Keep docs current" ScheduledAgent binding.
"""

from __future__ import annotations

from typing import Any

from logging_config import logger


async def dev_reset() -> dict[str, Any]:
    """Wipe + seed. Returns {"deleted": {label: n}, "seeded": {...}}."""
    from neomodel import adb

    from domains.checked.models import Checked
    from domains.comments.models import Comment
    from domains.delivery.models import FindingResolution, PullRequest, Verdict
    from domains.docs.models import Doc, DocEdit
    from domains.findings.models import Finding
    from domains.agents.models import ScheduledAgent
    from domains.runs.models import Run
    from domains.memory.models import Memory
    from domains.tickets.models import Ticket

    deleted: dict[str, int] = {}

    async def wipe(cls) -> None:
        name = cls.__name__
        deleted[name] = 0
        for node in await cls.nodes.all():
            await node.delete()
            deleted[name] += 1

    for cls in (
        Doc,
        DocEdit,
        Memory,
        Checked,
        Verdict,
        FindingResolution,
        PullRequest,
        Comment,
        Ticket,
        Finding,
        Run,
        ScheduledAgent,
    ):
        await wipe(cls)

    # Legacy labels whose Python models are deleted; purge stragglers.
    for label in (
        "Knowledge",
        "CoverageRecord",
        "Session",
        "Execution",
        "ExecutionEnvironment",
        "MapNode",
        "MapEdge",
    ):
        results, _ = await adb.cypher_query(
            f"MATCH (n:{label}) DETACH DELETE n RETURN count(n)"
        )
        count = int(results[0][0]) if results else 0
        if count:
            deleted[f"{label} (legacy)"] = count

    seeded: dict[str, Any] = {}

    # Tenancy: kept repositories must carry an org (multi-tenancy phase 2).
    # Dev runs in the fixed local org — stamp any repo missing one and make
    # sure the Organization node exists. Fresh registrations are stamped at
    # creation; this only catches graphs from before the tenancy cutover.
    from domains.organizations.models import LOCAL_ORG_UID, Organization

    if await Organization.nodes.get_or_none(uid=LOCAL_ORG_UID) is None:
        await Organization(uid=LOCAL_ORG_UID, name="Local").save()
    stamped_rows, _ = await adb.cypher_query(
        "MATCH (r:Repository) WHERE r.org_uid IS NULL OR r.org_uid = '' "
        "SET r.org_uid = $org RETURN count(r)",
        {"org": LOCAL_ORG_UID},
    )
    seeded["repos_stamped_local_org"] = int(stamped_rows[0][0]) if stamped_rows else 0

    # Seed everything a fresh install has, through the one registry (SYNC so a
    # reset also rolls shipped-default improvements forward). Only the platform
    # group: dev_reset KEEPS LLMProviders and the User, so their dev seeders
    # must not run here and clobber configured credentials.
    from infrastructure.seeding import SeedMode, run_seeders, summarize

    seed_results = await run_seeders(SeedMode.SYNC)
    seeded.update(summarize(seed_results))

    logger.info(
        f"dev reset complete: deleted={deleted} seeded={seeded}",
        extra={"tag": "dev-reset"},
    )
    return {"deleted": deleted, "seeded": seeded}
