"""Explicit GitHub repository registration (§7).

Repositories enter OpenSweep by SELECTION: the operator picks from the repos
their GitHub App installations grant access to (GET
/api/v1/github/app/available-repos → POST /api/v1/github/app/register-repo).
Installation webhooks only LINK/UNLINK installations onto repos that are
already registered — they never create Repository nodes.

This module owns the shared registration primitive plus the pure slug
helpers, so the endpoint (and any future flow) registers repos identically.
"""

from __future__ import annotations

import re
from uuid import uuid4

from domains.repositories.models import Repository
from infrastructure.audit import write_audit


def slug_for_repo_name(name: str) -> str:
    """GitHub repo name → OpenSweep slug: lowercase, [a-z0-9-], collapsed."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "repo"


def dedupe_slug(base: str, taken: set[str]) -> str:
    """`base`, or the first free `base-2`, `base-3`, … suffix."""
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


async def register_github_repo(
    *,
    org_uid: str,
    owner: str,
    name: str,
    repo_id: int | None = None,
    default_branch: str = "",
    installation_id: int | None = None,
    connection_uid: str | None = None,
    description: str = "",
) -> Repository:
    """Create one `mode=github` Repository node (deduped slug) and audit it.

    Registered into the caller's org — the tenancy root. Slugs are deduped
    within the org only. Callers verify eligibility first (repo belongs to
    the installation/connection, not already registered in the caller's org —
    other orgs may hold their own node for the same GitHub repo) — this
    primitive just materialises the node.
    """
    taken_slugs = {r.slug for r in await Repository.nodes.filter(org_uid=org_uid)}
    slug = dedupe_slug(slug_for_repo_name(name), taken_slugs)
    connected = installation_id is not None or bool(connection_uid)
    node = Repository(
        uid=uuid4().hex,
        org_uid=org_uid,
        slug=slug,
        mode="github",
        provider="github",
        name=name,
        description=description or "",
        default_branch=default_branch or "main",
        github_owner=owner or None,
        github_repo=name,
        github_repo_id=int(repo_id) if repo_id is not None else None,
        github_installation_id=int(installation_id) if installation_id is not None else None,
        git_connection_uid=connection_uid or None,
        github_connection_status="connected" if connected else None,
    )
    await node.save()
    # KNOWLEDGE_V3: every repo starts with a pinned, empty conventions page
    # so agents always have a propose_doc_edit target (same as
    # RepositoryService.create). Best-effort — a doc-seeding hiccup must not
    # fail the registration (the `document` playbook recreates it later).
    try:
        from domains.docs.services.doc_service import seed_conventions_doc
        from domains.agents.services.scheduled_agent_service import (
            seed_audit_agents,
            seed_audit_stale,
            seed_keep_docs_current,
        )

        await seed_conventions_doc(node.uid)
        await seed_keep_docs_current(node.uid)
        await seed_audit_stale(node.uid)
        await seed_audit_agents(node.uid)
    except Exception as exc:  # noqa: BLE001
        from logging_config import logger

        logger.warning(
            f"conventions page seed failed for {slug}: {type(exc).__name__}: {exc}",
            extra={"tag": "repositories"},
        )
    await write_audit(
        kind="repository.registered",
        subject_uid=node.uid,
        subject_type="Repository",
        actor_uid="operator",
        payload={
            "slug": slug,
            "full_name": f"{owner}/{name}" if owner else name,
            "installation_id": installation_id,
            "connection_uid": connection_uid or None,
        },
    )
    return node
