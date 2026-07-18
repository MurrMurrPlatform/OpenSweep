"""fastapi-mcp mounts — two separate surfaces.

PLATFORM.md draws a hard line between two transports:

1. **External-caller MCP** at `settings.MCP_MOUNT_PATH` (default `/mcp`).
   For third-party clients / ad-hoc agents that want to query or curate
   OpenSweep from outside. Curated read + standard CRUD operations only.

2. **Platform-tool MCP** at `settings.MCP_PLATFORM_TOOL_MOUNT_PATH`
   (default `/mcp/platform`). The 7 write tools executors use to push
   structured output back to OpenSweep. `claude_code`'s MCP bridge points at
   this mount via `mcp_bridge.write_claude_mcp_config`.

The two mounts are kept distinct so a third-party hitting `/mcp` can't
forge findings/knowledge under another Run's provenance.
"""

from fastapi import FastAPI

from config import settings
from logging_config import logger

# Curated external surface. Read + standard CRUD only — no `opensweep_platform_*`.
OPENSWEEP_MCP_OPERATIONS = [
    # Repos
    "opensweep_list_repositories",
    "opensweep_get_repository",
    # Findings
    "opensweep_list_findings",
    "opensweep_get_finding",
    "opensweep_file_finding",
    "opensweep_find_similar_finding",
    "opensweep_dismiss_finding",
    "opensweep_acknowledge_finding",
    "opensweep_wont_fix_finding",
    "opensweep_mark_fixed_finding",
    # Freshness (Checked stamps)
    "opensweep_repository_freshness",
    # Investigations
    "opensweep_list_investigations",
    "opensweep_get_investigation",
    "opensweep_create_investigation",
    # Docs + memories
    "opensweep_list_docs",
    "opensweep_get_doc",
    "opensweep_create_doc",
    "opensweep_list_memories",
    # Run policies
    "opensweep_list_run_policies",
    "opensweep_get_run_policy",
    "opensweep_create_run_policy",
    # Audit
    "opensweep_list_audit_events",
    "opensweep_get_audit_event",
    # Dev flow — `opensweep connect`: local agents (Claude Code, Codex,
    # OpenCode) pull tickets/threads/plans/test notes and report back.
    # Read-heavy by design; the only writes are comments (discussion) —
    # ticket status, plan approval, and merges stay in the UI.
    "opensweep_ticket_list",
    "opensweep_ticket_get",
    "opensweep_thread_list",
    "opensweep_thread_get",
    "opensweep_list_pull_requests",
    "opensweep_get_pull_request",
    "opensweep_comment_list",
    "opensweep_comment_create",
]


# Headers fastapi-mcp forwards from the MCP connection into each internal
# tool invocation (default is ['authorization'] ONLY). The internal call
# re-enters the ASGI app and passes through TokenAuthMiddleware, so without
# X-OpenSweep-Auth + X-OpenSweep-Run-Uid every tool call 401s even though the MCP
# handshake itself authenticated fine.
MCP_FORWARD_HEADERS = [
    "authorization",
    "x-opensweep-auth",
    "x-opensweep-run-uid",
]


# Executor-facing write surface — NEVER include this in the external mount.
OPENSWEEP_PLATFORM_TOOL_OPERATIONS = [
    # Writers
    "opensweep_platform_create_finding",
    "opensweep_platform_update_finding",
    "opensweep_platform_propose_doc_edit",
    "opensweep_platform_confirm_doc_current",
    "opensweep_platform_write_memory",
    "opensweep_platform_attach_artifact",
    "opensweep_platform_complete_run",
    # Deep-scan Analysis authoring — the agent builds the report incrementally
    "opensweep_platform_upsert_analysis",
    "opensweep_platform_set_analysis_section",
    "opensweep_platform_add_analysis_note",
    "opensweep_platform_ask_question",
    # Delivery — PR convergence ledger (PLATFORM_V2_DESIGN.md §11)
    "opensweep_platform_get_convergence_state",
    "opensweep_platform_list_pr_resolutions",
    "opensweep_platform_bind_finding_to_pr",
    "opensweep_platform_attach_fix",
    "opensweep_platform_verify_resolution",
    "opensweep_platform_request_waiver",
    "opensweep_platform_submit_verdict",
    "opensweep_platform_submit_finding_verification",
    "opensweep_platform_get_merge_policy",
    "opensweep_platform_list_open_pull_requests",
    # Tickets (§15 Phase 2) — agents may PROPOSE work (origin forced to
    # agent-proposal, status forced to backlog) and read tickets. There is
    # deliberately NO transition tool: Gate 1 (backlog → todo) is human-only.
    "opensweep_platform_create_ticket",
    "opensweep_platform_update_ticket",
    "opensweep_platform_get_ticket",
    "opensweep_platform_list_tickets",
    # Grouping — agents may PROPOSE batching related tickets under one parent;
    # approval (which materializes the parent) is human-only, like Gate 1.
    "opensweep_platform_propose_ticket_group",
    # Threads (unified dev flow) — the session agent persists its plan
    # (drafts only; approval is human-only) and asks the user structured
    # questions the thread UI renders as answer cards.
    "opensweep_platform_submit_thread_plan",
    "opensweep_platform_ask_user",
    # Comments — the human↔agent conversation on any data item. Agents read
    # the thread (also injected into run briefings) and reply in-thread;
    # @opensweep-summoned runs MUST answer via add_comment.
    "opensweep_platform_list_comments",
    "opensweep_platform_add_comment",
    # News radar (news-scout): file NewsItems only — news→finding conversion
    # is a HUMAN action. web_search / fetch_url are read-only against the
    # open internet (SSRF-guarded) and never touch OpenSweep state or the repo.
    "opensweep_platform_create_news_item",
    "opensweep_platform_web_search",
    "opensweep_platform_fetch_url",
    # Read-only OpenSweep-data tools (look-before-write contract)
    "opensweep_platform_read_list_docs",
    "opensweep_platform_read_doc",
    "opensweep_platform_read_search_memory",
    "opensweep_platform_read_list_findings",
    "opensweep_platform_read_get_finding",
    "opensweep_platform_read_search_findings",
    "opensweep_platform_read_list_news_items",
    "opensweep_platform_read_list_interests",
]


def mount_mcp(app: FastAPI) -> None:
    if not settings.MCP_ENABLED:
        logger.info("MCP disabled via settings", extra={"tag": "mcp"})
        return

    # Config sanity: Zitadel-only auth without a run-token secret means every
    # executor callback to the platform mount 401s — the SSE endpoint still
    # "connects", so the failure mode is a server that lists zero tools.
    from infrastructure.run_tokens import run_token_config_error

    config_error = run_token_config_error()
    if config_error:
        logger.error(f"MCP executor callbacks broken: {config_error}", extra={"tag": "mcp"})
    try:
        from fastapi_mcp import FastApiMCP
    except ImportError:
        logger.warning(
            "fastapi-mcp not installed — MCP surface unavailable", extra={"tag": "mcp"}
        )
        return

    # External-caller MCP is deferred for v1 — no consumer yet, and exposing 23
    # CRUD tools to anyone with a session cookie is a footgun. Re-enable by
    # un-skipping this block when there's a real third-party agent that needs
    # to query OpenSweep.
    if getattr(settings, "MCP_EXTERNAL_ENABLED", False):
        try:
            external = FastApiMCP(
                app,
                name="OpenSweep MCP",
                description=(
                    "Cost-aware repo intelligence — external curated read + CRUD "
                    "surface. Executors use the platform-tool mount, not this."
                ),
                include_operations=OPENSWEEP_MCP_OPERATIONS,
                headers=MCP_FORWARD_HEADERS,
            )
            external.mount(mount_path=settings.MCP_MOUNT_PATH)
            logger.info(
                f"MCP (external) mounted at {settings.MCP_MOUNT_PATH} "
                f"with {len(OPENSWEEP_MCP_OPERATIONS)} tools",
                extra={"tag": "mcp"},
            )
        except Exception as exc:
            logger.warning(f"External MCP mount skipped: {exc}", extra={"tag": "mcp"})

    # Platform-tool mount — narrow, write-only surface for delegated executors.
    platform_mount_path = getattr(
        settings, "MCP_PLATFORM_TOOL_MOUNT_PATH", "/mcp/platform"
    )
    try:
        platform = FastApiMCP(
            app,
            name="OpenSweep Platform Tools",
            description=(
                "Platform tool surface — the write tools (create_finding, "
                "update_finding, propose_doc_edit, confirm_doc_current, "
                "write_memory, attach_artifact, complete_run) that "
                "delegated executors call to push structured output back."
            ),
            include_operations=OPENSWEEP_PLATFORM_TOOL_OPERATIONS,
            headers=MCP_FORWARD_HEADERS,
        )
        platform.mount(mount_path=platform_mount_path)
        logger.info(
            f"MCP (platform-tools) mounted at {platform_mount_path} "
            f"with {len(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)} tools",
            extra={"tag": "mcp"},
        )
    except Exception as exc:
        logger.warning(
            f"Platform-tools MCP mount skipped: {exc}", extra={"tag": "mcp"}
        )
