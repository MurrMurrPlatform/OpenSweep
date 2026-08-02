"""News service — list, get, create (dedupe-merge), save/dismiss, convert.

Conversion to a Finding is a human-only action: the news-scout agent never
files findings, it only files NewsItems. convert_to_finding routes through
FindingService.file_finding so converted items get the full finding
machinery (dedupe, refine/verify, promote-to-ticket) for free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.findings.schemas import FileFindingRequest, FindingDTO, normalize_tags
from domains.findings.services.finding_service import FindingService
from domains.news.models import NewsItem
from domains.news.schemas import (
    ConvertNewsRequest,
    CreateNewsItemRequest,
    NewsCategory,
    NewsItemDTO,
    NewsSource,
    NewsStatus,
    UpdateNewsItemRequest,
)
from domains.news.services.dedupe import build_news_dedupe_key
from infrastructure.audit import write_audit


def news_item_to_dto(n: NewsItem) -> NewsItemDTO:
    return NewsItemDTO(
        uid=n.uid,
        repository_uid=n.repository_uid,
        title=n.title,
        url=n.url or "",
        source=NewsSource(n.source or "manual"),
        category=NewsCategory(n.category or "industry"),
        summary=n.summary or "",
        relevance=n.relevance or "",
        tags=list(n.tags or []),
        published_at=n.published_at,
        status=NewsStatus(n.status or "new"),
        converted_finding_uid=n.converted_finding_uid or "",
        dedupe_key=n.dedupe_key,
        source_run_uid=n.source_run_uid,
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


class NewsService:
    async def list(
        self,
        *,
        repository_uids: list[str],
        category: str | None = None,
        status: str | None = None,
        source_run_uid: str | None = None,
    ) -> list[NewsItemDTO]:
        """News items in ``repository_uids`` — the caller's tenancy scope, so
        an empty list means an empty result, never "every item"."""
        if not repository_uids:
            return []
        filters: dict = {"repository_uid__in": repository_uids}
        if category:
            filters["category"] = category
        if status:
            filters["status"] = status
        if source_run_uid:
            filters["source_run_uid"] = source_run_uid
        out = [news_item_to_dto(n) for n in await NewsItem.nodes.filter(**filters)]
        out.sort(
            key=lambda n: n.updated_at
            or n.created_at
            or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return out

    async def get(self, uid: str) -> NewsItemDTO:
        return news_item_to_dto(await self.get_node(uid))

    async def get_node(self, uid: str) -> NewsItem:
        n = await NewsItem.nodes.get_or_none(uid=uid)
        if n is None:
            raise HTTPException(status_code=404, detail=f"News item {uid} not found")
        return n

    async def create(
        self, req: CreateNewsItemRequest, *, actor_uid: str | None = None
    ) -> tuple[NewsItemDTO, bool]:
        """Create (or dedupe-merge) a NewsItem.

        Returns (dto, deduplicated): deduplicated=True when the filing merged
        into an existing item instead of creating a new one."""
        dedupe = build_news_dedupe_key(
            repository_uid=req.repository_uid, url=req.url, title=req.title
        )
        existing = await NewsItem.nodes.get_or_none(dedupe_key=dedupe)
        if existing is not None:
            # Merge-on-refile: extend tags, fill narrative gaps only — never
            # overwrite richer earlier text; the human triage state stays put.
            existing.tags = normalize_tags([*(existing.tags or []), *(req.tags or [])])
            if not (existing.summary or "").strip() and req.summary:
                existing.summary = req.summary
            if not (existing.relevance or "").strip() and req.relevance:
                existing.relevance = req.relevance
            existing.updated_at = datetime.now(UTC)
            await existing.save()
            return news_item_to_dto(existing), True

        n = NewsItem(
            uid=uuid4().hex,
            repository_uid=req.repository_uid,
            title=req.title,
            url=req.url or "",
            source=req.source.value,
            category=req.category.value,
            summary=req.summary,
            relevance=req.relevance,
            tags=normalize_tags(req.tags),
            published_at=req.published_at,
            status=NewsStatus.NEW.value,
            dedupe_key=dedupe,
            source_run_uid=req.source_run_uid,
        )
        await n.save()
        await write_audit(
            kind="news.filed",
            subject_uid=n.uid,
            subject_type="NewsItem",
            actor_uid=actor_uid,
            repository_uid=n.repository_uid,
            payload={"category": n.category, "source": n.source, "url": n.url},
        )
        return news_item_to_dto(n), False

    async def update(
        self, uid: str, req: UpdateNewsItemRequest, *, actor_uid: str | None = None
    ) -> NewsItemDTO:
        n = await self.get_node(uid)
        fields = req.model_dump(exclude_unset=True)
        for key, value in fields.items():
            if key == "tags":
                value = normalize_tags(value)
            elif key == "category" and value is not None:
                value = value.value
            setattr(n, key, value)
        n.updated_at = datetime.now(UTC)
        await n.save()
        await write_audit(
            kind="news.edited",
            subject_uid=uid,
            subject_type="NewsItem",
            actor_uid=actor_uid,
            repository_uid=n.repository_uid,
            payload={"fields": sorted(fields.keys())},
        )
        return news_item_to_dto(n)

    async def _set_status(
        self,
        uid: str,
        status: NewsStatus,
        *,
        actor_uid: str | None = None,
        audit_kind: str,
    ) -> NewsItemDTO:
        n = await self.get_node(uid)
        n.status = status.value
        n.updated_at = datetime.now(UTC)
        await n.save()
        await write_audit(
            kind=audit_kind,
            subject_uid=uid,
            subject_type="NewsItem",
            actor_uid=actor_uid,
            repository_uid=n.repository_uid,
        )
        return news_item_to_dto(n)

    async def dismiss(self, uid: str, *, actor_uid: str | None = None) -> NewsItemDTO:
        return await self._set_status(
            uid, NewsStatus.DISMISSED, actor_uid=actor_uid, audit_kind="news.dismissed"
        )

    async def save_item(self, uid: str, *, actor_uid: str | None = None) -> NewsItemDTO:
        return await self._set_status(
            uid, NewsStatus.SAVED, actor_uid=actor_uid, audit_kind="news.saved"
        )

    async def convert_to_finding(
        self, uid: str, req: ConvertNewsRequest, *, actor_uid: str | None = None
    ) -> FindingDTO:
        n = await self.get_node(uid)
        if (n.status or "") == NewsStatus.CONVERTED.value or (
            n.converted_finding_uid or ""
        ).strip():
            raise HTTPException(
                status_code=409, detail=f"News item {uid} is already converted"
            )
        description_parts = [n.summary or ""]
        if (n.relevance or "").strip():
            description_parts.append(f"**Why it matters for this repo:** {n.relevance}")
        if (n.url or "").strip():
            description_parts.append(f"Source: {n.url}")
        finding = await FindingService().file_finding(
            FileFindingRequest(
                repository_uid=n.repository_uid,
                kind=req.kind,
                severity=req.severity,
                size=req.size,
                title=n.title,
                description="\n\n".join(p for p in description_parts if p.strip()),
                why_it_matters=n.relevance or "",
                tags=normalize_tags(
                    [*(n.tags or []), "news", n.category or "", *req.extra_tags]
                ),
                evidence={
                    "news_item_uid": n.uid,
                    "url": n.url or "",
                    "source": n.source or "manual",
                },
                executor="manual",
            ),
            actor_uid=actor_uid,
        )
        n.status = NewsStatus.CONVERTED.value
        n.converted_finding_uid = finding.uid
        n.updated_at = datetime.now(UTC)
        await n.save()
        await write_audit(
            kind="news.converted",
            subject_uid=uid,
            subject_type="NewsItem",
            actor_uid=actor_uid,
            repository_uid=n.repository_uid,
            payload={"finding_uid": finding.uid, "kind": req.kind.value},
        )
        return finding

    async def delete(self, uid: str, *, actor_uid: str | None = None) -> None:
        n = await self.get_node(uid)
        repository_uid = n.repository_uid
        await n.delete()
        await write_audit(
            kind="news.deleted",
            subject_uid=uid,
            subject_type="NewsItem",
            actor_uid=actor_uid,
            repository_uid=repository_uid,
        )
