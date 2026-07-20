"""Repository CRUD — GitHub-only, org-scoped (multi-tenancy phase 2).

Every method takes the caller's org_uid and refuses to see repositories
outside it (404 — existence never leaks across tenants). Slug uniqueness is
per org, enforced here (Neo4j Community has no composite constraints).

Internal machinery that operates on behalf of an already-scoped Run (workers,
lifecycle) resolves Repository nodes directly by uid — the run inherited its
repo from an org-checked API call.
"""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.repositories.models import Repository
from domains.repositories.schemas import (
    CreateRepositoryRequest,
    RepositoryDTO,
    UpdateRepositoryRequest,
)


class RepositoryService:
    async def list_repositories(self, org_uid: str) -> list[RepositoryDTO]:
        repos = await Repository.nodes.filter(org_uid=org_uid)
        return [_to_dto(r) for r in repos]

    async def get_repository(self, uid: str, org_uid: str) -> RepositoryDTO:
        return _to_dto(await self.get_repository_node(uid, org_uid))

    async def get_repository_by_slug(self, slug: str, org_uid: str) -> RepositoryDTO:
        r = await Repository.nodes.get_or_none(slug=slug, org_uid=org_uid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Repository slug '{slug}' not found")
        return _to_dto(r)

    async def get_repository_node(self, uid: str, org_uid: str) -> Repository:
        r = await Repository.nodes.get_or_none(uid=uid, org_uid=org_uid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Repository {uid} not found")
        return r

    async def create(self, req: CreateRepositoryRequest, org_uid: str) -> RepositoryDTO:
        existing = await Repository.nodes.get_or_none(slug=req.slug, org_uid=org_uid)
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Repository slug '{req.slug}' already exists")
        r = Repository(
            uid=uuid4().hex,
            org_uid=org_uid,
            slug=req.slug,
            mode=req.mode.value,
            provider="github",
            name=req.name,
            description=req.description,
            default_branch=req.default_branch,
            color_scheme=req.color_scheme,
            github_owner=req.github_owner,
            github_repo=req.github_repo,
        )
        await r.save()
        # KNOWLEDGE_V3: every repo starts with a pinned, empty conventions
        # page so agents always have a propose_doc_edit target, plus the
        # on-event "Keep docs current" Investigation (§9).
        from domains.docs.services.doc_service import seed_conventions_doc
        from domains.agents.services.scheduled_agent_service import (
            seed_audit_agents,
            seed_audit_stale,
            seed_keep_docs_current,
        )

        await seed_conventions_doc(r.uid)
        await seed_keep_docs_current(r.uid)
        await seed_audit_stale(r.uid)
        await seed_audit_agents(r.uid)
        return _to_dto(r)

    async def update(
        self, uid: str, req: UpdateRepositoryRequest, org_uid: str
    ) -> RepositoryDTO:
        r = await self.get_repository_node(uid, org_uid)
        data = req.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(r, field, value)
        r.updated_at = datetime.now(UTC)
        await r.save()
        return _to_dto(r)

    # Every label whose nodes carry a repository_uid. Kept in sync with the
    # wipe list in infrastructure/dev_reset.py — deleting a repository must
    # not leave orphans that no org can ever see again (they'd 404 forever yet
    # still surface in full-label scans).
    _CASCADE_LABELS = (
        "Doc",
        "DocEdit",
        "Memory",
        "Checked",
        "Verdict",
        "FindingResolution",
        "PullRequest",
        "Comment",
        "Ticket",
        "Finding",
        "Run",
        "ScheduledAgent",
    )

    async def delete(self, uid: str, org_uid: str) -> None:
        from neomodel import adb

        r = await self.get_repository_node(uid, org_uid)
        for label in self._CASCADE_LABELS:
            await adb.cypher_query(
                f"MATCH (n:{label} {{repository_uid: $uid}}) DETACH DELETE n",
                {"uid": uid},
            )
        await r.delete()


def _to_dto(r: Repository) -> RepositoryDTO:
    return RepositoryDTO(
        uid=r.uid,
        org_uid=r.org_uid,
        slug=r.slug,
        mode=r.mode,
        provider=getattr(r, "provider", None) or "github",
        name=r.name,
        description=r.description or "",
        default_branch=r.default_branch or "main",
        color_scheme=r.color_scheme or "indigo",
        is_active=bool(r.is_active),
        github_owner=r.github_owner,
        github_repo=r.github_repo,
        github_repo_id=r.github_repo_id,
        github_installation_id=r.github_installation_id,
        git_connection_uid=getattr(r, "git_connection_uid", None),
        github_connection_status=r.github_connection_status,
        last_synced_at=r.last_synced_at,
        metadata=dict(r.metadata or {}),
        kill_switch_active=bool(r.kill_switch_active),
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def repository_to_dto(r: Repository) -> RepositoryDTO:
    return _to_dto(r)
