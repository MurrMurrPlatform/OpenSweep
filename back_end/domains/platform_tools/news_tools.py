"""Platform tools: news radar — create_news_item + list_news_items + list_interests.

This is the news-scout's filing surface. The scout researches the open web
(via `web_search` / `fetch_url`), grounds what it finds in THIS repository's
stack, tickets, findings, and the user-entered Interests, then files each
relevant story as a NewsItem here. NewsItems never become Findings
automatically: conversion is a human-only action in the News UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from domains.news.schemas import (
    CreateNewsItemRequest,
    NewsCategory,
    NewsSource,
)
from domains.news.services.interest_service import InterestService
from domains.news.services.news_service import NewsService
from domains.platform_tools.create_finding import _enum_member


def _parse_published_at(value: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 parse — None/empty/garbage all collapse to None
    (published_at is nice-to-have metadata, never worth failing a filing)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


async def create_news_item(
    *,
    repository_uid: str,
    title: str,
    url: str = "",
    source: str = "manual",
    category: str = "industry",
    summary: str = "",
    relevance: str = "",
    tags: Optional[list[str]] = None,
    published_at: Optional[str] = None,
    source_run_uid: Optional[str] = None,
    executor: str = "",
    **_: Any,
) -> dict[str, Any]:
    """File a NewsItem on the repository's news radar.

    This is how the news-scout records a relevant story (trending repo, AI
    news, framework release, technique, research paper, tooling, industry
    move). Humans triage the radar; conversion of a news item into a Finding
    is a HUMAN-ONLY action — never file a Finding for news yourself.

    `category` is one of: trending-repo, ai-news, framework, technique,
    research, tooling, industry. `source` is where you found it: searxng,
    github, hackernews, arxiv, trendshift, or manual.

    `summary` (markdown) describes what the item itself is about;
    `relevance` (markdown) explains why THIS repository's team should care —
    that is the field humans triage by, so always ground it in the repo's
    actual stack, open findings, or Interests.

    `published_at` is an optional ISO-8601 timestamp of the original story.

    Idempotent: refiling the same story (same normalized URL, or same title
    when URL-less) merges tags and fills narrative gaps on the existing item
    instead of duplicating — the response's `deduplicated` flag tells you
    which path was taken.
    """
    del executor  # accepted from the envelope dispatcher; NewsItem records no executor
    _enum_member(NewsCategory, category, "category")
    _enum_member(NewsSource, source, "source")

    dto, deduplicated = await NewsService().create(
        CreateNewsItemRequest(
            repository_uid=repository_uid,
            title=title,
            url=url or "",
            source=NewsSource(source),
            category=NewsCategory(category),
            summary=summary,
            relevance=relevance,
            tags=list(tags or []),
            published_at=_parse_published_at(published_at),
            source_run_uid=source_run_uid,
        ),
        actor_uid=None,
    )
    return {
        "news_item_uid": dto.uid,
        "dedupe_key": dto.dedupe_key,
        "deduplicated": deduplicated,
    }


async def list_news_items(
    *,
    repository_uid: str,
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    **_: Any,
) -> list[dict[str, Any]]:
    """List NewsItems already on the repository's radar, newest first.

    Use BEFORE `create_news_item` (look-before-write): if a story is already
    filed — whatever its triage status — do not refile it under a different
    title. Optionally filter by `category` (trending-repo | ai-news |
    framework | technique | research | tooling | industry) and `status`
    (new | saved | dismissed | converted).
    """
    items = await NewsService().list(
        repository_uids=[repository_uid], category=category, status=status
    )
    return [
        {
            "uid": n.uid,
            "title": n.title,
            "url": n.url,
            "category": n.category.value,
            "status": n.status.value,
            "tags": list(n.tags or []),
        }
        for n in items[: max(int(limit), 0)]
    ]


async def list_interests(
    *,
    repository_uid: str,
    enabled_only: bool = True,
    **_: Any,
) -> list[dict[str, Any]]:
    """List the user-entered Interests the news scout must watch.

    Interests are explicit topics humans asked the scout to track, on top of
    whatever it derives from the repository itself. Read them at the START of
    a news scan and make sure every enabled interest gets search coverage.
    """
    interests = await InterestService().list(
        repository_uids=[repository_uid], enabled_only=enabled_only
    )
    return [
        {
            "uid": i.uid,
            "title": i.title,
            "details": i.details,
            "enabled": i.enabled,
        }
        for i in interests
    ]
