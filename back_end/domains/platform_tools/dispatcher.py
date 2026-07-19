"""Platform-tool dispatcher.

Single in-process entry point for executors that wish to invoke any of
the tracking-safe platform tools by name. Used by:
- the HTTP transport (`api/v1/platform_tools.py`) for non-Python executors
- the `internal_llm` adapter to call tools synchronously
- the MCP bridge in the `claude_code` adapter to route MCP tool calls
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from domains.platform_tools import (
    add_analysis_note,
    ask_question,
    ask_user,
    attach_artifact,
    complete_run,
    confirm_doc_current,
    create_finding,
    create_news_item,
    fetch_url,
    list_docs,
    list_interests,
    list_news_items,
    propose_doc_edit,
    read_doc,
    search_memory,
    set_analysis_section,
    submit_for_review,
    submit_thread_plan,
    update_finding,
    upsert_analysis,
    web_search,
    write_memory,
)


_TOOLS: dict[str, Callable[..., Awaitable[Any]]] = {
    "create_finding": create_finding,
    "update_finding": update_finding,
    "propose_doc_edit": propose_doc_edit,
    "confirm_doc_current": confirm_doc_current,
    "write_memory": write_memory,
    "list_docs": list_docs,
    "read_doc": read_doc,
    "search_memory": search_memory,
    "attach_artifact": attach_artifact,
    "complete_run": complete_run,
    "submit_thread_plan": submit_thread_plan,
    "submit_for_review": submit_for_review,
    "ask_user": ask_user,
    "upsert_analysis": upsert_analysis,
    "set_analysis_section": set_analysis_section,
    "add_analysis_note": add_analysis_note,
    "ask_question": ask_question,
    # News radar (news-scout) + open-web research
    "create_news_item": create_news_item,
    "list_news_items": list_news_items,
    "list_interests": list_interests,
    "web_search": web_search,
    "fetch_url": fetch_url,
}


def tool_names() -> list[str]:
    return sorted(_TOOLS.keys())


async def dispatch(tool: str, **kwargs: Any) -> Any:
    """Invoke a tool by name with kwargs. Raises KeyError on unknown tool."""
    fn = _TOOLS[tool]
    return await fn(**kwargs)
