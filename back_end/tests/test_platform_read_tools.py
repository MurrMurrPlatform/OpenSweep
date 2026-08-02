"""Tests that the new OpenSweep-data read tools are registered + tracking-safe.

The actual Neo4j-backed calls are exercised in integration tests; here we
confirm the registry surface and signatures.
"""

import inspect

from domains.platform_tools.read_tools import READ_TOOLS, read_tool_names


def test_opensweep_read_tools_registered():
    expected = {
        "list_docs",
        "read_doc",
        "search_memory",
        "opensweep_list_findings",
        "opensweep_search_findings",
        "opensweep_get_finding",
        # News radar + open-web research (news-scout)
        "list_news_items",
        "list_interests",
        "web_search",
        "fetch_url",
    }
    actual = set(READ_TOOLS.keys())
    missing = expected - actual
    assert not missing, f"missing read tools: {missing}"


def test_file_read_tools_still_present():
    """internal_llm relies on the file-reading tools — don't remove them."""
    legacy = {"read_code", "trace", "prior_findings"}
    assert legacy <= set(READ_TOOLS.keys())


def test_knowledge_read_tools_are_gone():
    """KNOWLEDGE_V3: the Knowledge node store no longer exists."""
    removed = {"read_knowledge", "opensweep_list_knowledge", "opensweep_get_knowledge", "opensweep_search_knowledge"}
    assert not (removed & set(READ_TOOLS.keys()))


def test_all_read_tools_are_async_callables():
    for name, fn in READ_TOOLS.items():
        assert callable(fn), f"{name} is not callable"
        assert inspect.iscoroutinefunction(fn), f"{name} is not async"


def test_read_tool_names_sorted():
    names = read_tool_names()
    assert names == sorted(names)


def test_read_doc_route_matches_path_like_slugs():
    """Doc slugs are path-like ("backend/queue-workers"); the route must use the
    `:path` convertor or every folder page 404s at the router.

    This was live: read_doc 404'd on ~30 of 35 pages for MCP-transport
    executors, which is why document runs silently degraded into code review.
    Asserting on the compiled regex rather than the declared string so a
    refactor that changes the spelling but keeps the semantics still passes.
    """
    from starlette.routing import compile_path

    from api.v1.platform_read import router

    route = next(
        r for r in router.routes if getattr(r, "name", "") == "read_doc"
    )
    regex, path_format, _ = compile_path(route.path)

    assert regex.match("/api/v1/platform-read/docs/backend/domains/tickets")
    assert regex.match("/api/v1/platform-read/docs/conventions")
    # fastapi-mcp substitutes into the OpenAPI path_format with a plain
    # `path.replace("{slug}", value)` and no URL-encoding, so the convertor
    # must stay invisible there or the generated tool posts a literal
    # "{slug:path}" and 404s just as hard.
    assert path_format.endswith("/docs/{slug}")


def test_repository_by_slug_route_matches_owner_name_slugs():
    """Repository slugs are stored unslugified and are "owner/name" shaped."""
    from starlette.routing import compile_path

    from api.v1.repositories import router

    route = next(
        r for r in router.routes if getattr(r, "name", "") == "get_repository_by_slug"
    )
    regex, _, _ = compile_path(route.path)
    assert regex.match("/api/v1/repositories/by-slug/acme/api")
