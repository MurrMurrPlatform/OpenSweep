"""Platform-tool dispatcher.

Single in-process entry point for executors that wish to invoke any of
the tracking-safe platform tools by name. Used by:
- the HTTP transport (`api/v1/platform_tools.py`) for non-Python executors
- the `internal_llm` adapter to call tools synchronously
- the MCP bridge in the `claude_code` adapter to route MCP tool calls

Each registry entry carries a one-line description used by the executor
prompt kit (`domains/executors/prompt_kit.py`) to render tool lists — the
registry is the single source, so prompts can never drift from the surface.
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
    propose_area_edit,
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

_ToolFn = Callable[..., Awaitable[Any]]

_TOOLS: dict[str, tuple[_ToolFn, str]] = {
    "create_finding": (
        create_finding,
        "file a Finding (bug, docs gap, risk, or improvement) with concrete "
        "evidence; duplicates fold into the existing Finding",
    ),
    "update_finding": (
        update_finding,
        "update facets on an existing Finding by uid (status, severity, "
        "description, …)",
    ),
    "propose_doc_edit": (
        propose_doc_edit,
        "propose a full replacement body for a documentation page; lands as "
        "a pending edit for human review",
    ),
    "propose_area_edit": (
        propose_area_edit,
        "propose one area (subsystem | feature | ignore) for the "
        "repository's area map; lands as a pending edit for human review",
    ),
    "confirm_doc_current": (
        confirm_doc_current,
        "record that a documentation page was checked against the code and "
        "is still accurate",
    ),
    "write_memory": (
        write_memory,
        "record a small durable fact future runs should know (gotcha, "
        "decision, non-obvious constraint) — one paragraph",
    ),
    "list_docs": (
        list_docs,
        "list this repository's documentation pages (slug, title, summary, "
        "pinned, stale)",
    ),
    "read_doc": (
        read_doc,
        "fetch one documentation page's full body by slug",
    ),
    "search_memory": (
        search_memory,
        "full-text search this repository's memories (facts prior runs "
        "learned that the code cannot express)",
    ),
    "attach_artifact": (
        attach_artifact,
        "attach non-patch supporting output (logs, traces, notes) to a run, "
        "finding, doc, memory, ticket, or pull request",
    ),
    "complete_run": (
        complete_run,
        "finish the run with the end-of-run report (summary, did / skipped / "
        "succeeded / failed / next_steps, plus coverage)",
    ),
    "submit_thread_plan": (
        submit_thread_plan,
        "persist the thread session's implementation plan and pause for "
        "approval",
    ),
    "submit_for_review": (
        submit_for_review,
        "signal thread work is complete and ready for platform validation "
        "and review",
    ),
    "ask_user": (
        ask_user,
        "ask the user a structured question and pause the thread for their "
        "answer",
    ),
    "upsert_analysis": (
        upsert_analysis,
        "create or update the deep-scan Analysis (verdict + scorecard) for "
        "this run",
    ),
    "set_analysis_section": (
        set_analysis_section,
        "set or replace one narrative section of the deep-scan Analysis",
    ),
    "add_analysis_note": (
        add_analysis_note,
        "append one coverage/strength/validation row to the deep-scan "
        "Analysis",
    ),
    "ask_question": (
        ask_question,
        "record an unresolved question for a human on the deep-scan Analysis",
    ),
    # News radar (news-scout) + open-web research
    "create_news_item": (
        create_news_item,
        "file a NewsItem on the repository's news radar (news-scan runs "
        "only; news→finding conversion is human-only)",
    ),
    "list_news_items": (
        list_news_items,
        "list NewsItems already on the radar, newest first — dedupe before "
        "filing",
    ),
    "list_interests": (
        list_interests,
        "list the user-entered Interests the news scout must watch",
    ),
    "web_search": (
        web_search,
        "search the open web; read-only — results are leads, not facts",
    ),
    "fetch_url": (
        fetch_url,
        "fetch one public http(s) URL's content (read-only, SSRF-guarded)",
    ),
}


def tool_names() -> list[str]:
    return sorted(_TOOLS.keys())


def tool_descriptions() -> dict[str, str]:
    """name → one-line description, for prompt rendering."""
    return {name: desc for name, (_fn, desc) in _TOOLS.items()}


async def dispatch(tool: str, **kwargs: Any) -> Any:
    """Invoke a tool by name with kwargs. Raises KeyError on unknown tool."""
    fn, _desc = _TOOLS[tool]
    return await fn(**kwargs)
