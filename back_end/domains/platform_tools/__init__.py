"""Platform tool surface -- executor to platform.

Executors call into these to push durable structured output back. Findings
keep the record-permission dial; doc edits always land as pending DocEdits,
memories are live immediately (KNOWLEDGE_V3).
"""

from domains.platform_tools.add_analysis_note import add_analysis_note
from domains.platform_tools.ask_question import ask_question
from domains.platform_tools.attach_artifact import attach_artifact
from domains.platform_tools.complete_run import complete_run
from domains.platform_tools.create_finding import create_finding
from domains.platform_tools.docs_tools import (
    confirm_doc_current,
    list_docs,
    propose_doc_edit,
    read_doc,
)
from domains.platform_tools.memory_tools import search_memory, write_memory
from domains.platform_tools.news_tools import (
    create_news_item,
    list_interests,
    list_news_items,
)
from domains.platform_tools.set_analysis_section import set_analysis_section
from domains.platform_tools.submit_thread_plan import submit_thread_plan
from domains.platform_tools.update_finding import update_finding
from domains.platform_tools.upsert_analysis import upsert_analysis
from domains.platform_tools.web_tools import fetch_url, web_search

__all__ = [
    "create_finding",
    "update_finding",
    "propose_doc_edit",
    "confirm_doc_current",
    "write_memory",
    "list_docs",
    "read_doc",
    "search_memory",
    "attach_artifact",
    "complete_run",
    "submit_thread_plan",
    # Deep-scan Analysis authoring
    "upsert_analysis",
    "set_analysis_section",
    "add_analysis_note",
    "ask_question",
    # News radar (news-scout) + open-web research
    "create_news_item",
    "list_news_items",
    "list_interests",
    "web_search",
    "fetch_url",
]
