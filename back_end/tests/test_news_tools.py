"""News radar tools — wiring + DB-free validation.

Mirrors test_analysis_tools.py: the happy path touches Neo4j; here we assert
the news/web tools are registered on every surface an agent reaches
(dispatcher, MCP ops, HTTP routes, harness prompts) and that input
validation rejects bad args BEFORE any DB write.
"""

import pytest
from fastapi import HTTPException

from domains.platform_tools.dispatcher import tool_names
from domains.platform_tools.news_tools import create_news_item
from domains.platform_tools.web_tools import web_search

NEWS_TOOLS = {
    "create_news_item",
    "list_news_items",
    "list_interests",
    "web_search",
    "fetch_url",
}

# create_news_item/web_search/fetch_url are write-surface ops; the two list
# tools are exposed on the read surface (opensweep_platform_read_*).
NEWS_MCP_OPERATIONS = {
    "opensweep_platform_create_news_item",
    "opensweep_platform_web_search",
    "opensweep_platform_fetch_url",
    "opensweep_platform_read_list_news_items",
    "opensweep_platform_read_list_interests",
}


def test_tools_registered_in_dispatcher():
    assert NEWS_TOOLS <= set(tool_names())


def test_tools_registered_as_mcp_operations():
    from mcp_app import OPENSWEEP_PLATFORM_TOOL_OPERATIONS

    assert NEWS_MCP_OPERATIONS <= set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)


def test_harness_prompts_list_the_tools():
    # News-scout runs execute on the harness executors, so both read prompts
    # must advertise the news tool surface.
    from domains.executors.prompt_kit import system_prompt

    for kind in ("claude_code_read", "cli_tracking"):
        prompt = system_prompt(kind)
        for t in NEWS_TOOLS:
            assert t in prompt, f"{t} missing from {kind} prompt"


def test_http_routes_exist_on_platform_tools_router():
    import api.v1.platform_tools as pt

    paths = {r.path for r in pt.router.routes}
    assert "/api/v1/platform-tools/create-news-item" in paths
    assert "/api/v1/platform-tools/web-search" in paths
    assert "/api/v1/platform-tools/fetch-url" in paths


def test_http_routes_exist_on_platform_read_router():
    import api.v1.platform_read as pr

    paths = {r.path for r in pr.router.routes}
    assert "/api/v1/platform-read/news-items" in paths
    assert "/api/v1/platform-read/interests" in paths
    op_ids = {getattr(r, "operation_id", None) for r in pr.router.routes}
    assert "opensweep_platform_read_list_news_items" in op_ids
    assert "opensweep_platform_read_list_interests" in op_ids


async def _expect_422(coro):
    with pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == 422


async def test_create_news_item_rejects_bad_category_before_db():
    await _expect_422(
        create_news_item(repository_uid="r", title="t", category="bogus")
    )


async def test_create_news_item_rejects_bad_source_before_db():
    await _expect_422(
        create_news_item(repository_uid="r", title="t", source="carrier-pigeon")
    )


async def test_web_search_rejects_invalid_mode():
    await _expect_422(web_search(query="q", mode="gopher"))


def test_scan_fallback_intent_names_the_tools_and_the_human_only_rule():
    from api.v1.news import _SCAN_FALLBACK_INTENT

    for t in ("list_interests", "web_search", "create_news_item"):
        assert t in _SCAN_FALLBACK_INTENT
    # Conversion of news into findings is human-only — the fallback intent
    # must carry the prohibition even when the seeded variant is missing.
    assert "NEVER call" in _SCAN_FALLBACK_INTENT
    assert "create_finding" in _SCAN_FALLBACK_INTENT
