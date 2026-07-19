"""The external MCP tool surface speaks the new PLATFORM.md vocabulary.

This guards against accidentally re-introducing the legacy Check / Ticket /
Suggestion / Verification operations that were removed when the platform
moved to the Investigation / Knowledge / RunPolicy primitives.
"""

from mcp_app import OPENSWEEP_MCP_OPERATIONS, OPENSWEEP_PLATFORM_TOOL_OPERATIONS

EXPECTED = {
    "opensweep_list_repositories", "opensweep_get_repository",
    "opensweep_list_findings", "opensweep_get_finding", "opensweep_file_finding",
    "opensweep_find_similar_finding",
    "opensweep_dismiss_finding", "opensweep_acknowledge_finding",
    "opensweep_wont_fix_finding", "opensweep_mark_fixed_finding",
    "opensweep_repository_freshness",
    "opensweep_list_investigations", "opensweep_get_investigation", "opensweep_create_investigation",
    "opensweep_list_docs", "opensweep_get_doc", "opensweep_create_doc",
    "opensweep_list_memories",
    "opensweep_list_run_policies", "opensweep_get_run_policy", "opensweep_create_run_policy",
    # Dev flow (`opensweep connect`) — local-agent surface.
    "opensweep_ticket_list", "opensweep_ticket_get",
    "opensweep_thread_list", "opensweep_thread_get",
    "opensweep_list_pull_requests", "opensweep_get_pull_request",
    "opensweep_comment_list", "opensweep_comment_create",
}

LEGACY_FORBIDDEN = {
    # Pre-PLATFORM.md operations that must not return.
    "opensweep_list_checks", "opensweep_get_check", "opensweep_run_check", "opensweep_run_map_refresh",
    "opensweep_get_area_health",
    "opensweep_list_tickets", "opensweep_get_ticket",
    "opensweep_assign_ticket", "opensweep_update_ticket_state",
    "opensweep_list_verifications", "opensweep_get_verification", "opensweep_verify_ticket",
    "opensweep_get_repo_map", "opensweep_get_map_node", "opensweep_approve_finding",
    "opensweep_platform_propose_map_change",
    "opensweep_platform_read_list_map_nodes", "opensweep_platform_read_get_map_node",
    "opensweep_platform_read_search_map_nodes", "opensweep_platform_read_list_map_edges",
    "opensweep_list_suggestions", "opensweep_get_suggestion",
    "opensweep_accept_suggestion", "opensweep_dismiss_suggestion", "opensweep_recompute_suggestions",
    "opensweep_evaluate_policy", "opensweep_promote_finding", "opensweep_keep_finding",
    "opensweep_platform_attach_patch_to_finding", "opensweep_approve_and_apply_patch",
    "opensweep_revert_apply_job",
    # KNOWLEDGE_V3: the Knowledge/Coverage primitives are deleted.
    "opensweep_list_knowledge", "opensweep_get_knowledge", "opensweep_create_knowledge",
    "opensweep_maintain_knowledge",
    "opensweep_list_coverage", "opensweep_get_coverage_matrix",
}


def test_expected_operations_are_mounted():
    actual = set(OPENSWEEP_MCP_OPERATIONS)
    missing = EXPECTED - actual
    assert not missing, f"MCP missing expected operations: {missing}"


def test_legacy_operations_are_removed():
    actual = set(OPENSWEEP_MCP_OPERATIONS)
    leaked = LEGACY_FORBIDDEN & actual
    assert not leaked, f"MCP still exposes legacy operations: {leaked}"


def test_no_duplicates():
    assert len(OPENSWEEP_MCP_OPERATIONS) == len(set(OPENSWEEP_MCP_OPERATIONS))


def test_platform_tools_are_tracking_safe():
    """All platform-mounted tools must be tracking-safe.

    Writers (`create_finding`, `update_finding`, `propose_doc_edit`,
    `confirm_doc_current`, `write_memory`, `attach_artifact`, `complete_run`)
    record OpenSweep artifacts; they never mutate the source repository.

    Read-only `opensweep_platform_read_*` tools query OpenSweep's own data store —
    also tracking-safe. They are required by the look-before-write contract
    so executors can dedupe instead of accumulating duplicates.

    Any tool name that doesn't match this allow-list of prefixes is a leak.
    """
    actual = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)
    writers = {
        "opensweep_platform_create_finding",
        "opensweep_platform_update_finding",
        "opensweep_platform_propose_doc_edit",
        "opensweep_platform_confirm_doc_current",
        "opensweep_platform_write_memory",
        "opensweep_platform_attach_artifact",
        "opensweep_platform_complete_run",
        # News scout — writes a OpenSweep NewsItem node (radar entry), same class
        # of write as create_finding. News→finding conversion stays HUMAN-only,
        # so this cannot mint findings.
        "opensweep_platform_create_news_item",
    }
    missing_writers = writers - actual
    assert not missing_writers, f"writer tools missing: {missing_writers}"
    # Deep-scan Analysis authoring — write OpenSweep's own Analysis artifact
    # (verdict, report sections, coverage notes, questions), never the source
    # repository. Tracking-safe, same as the finding/doc/memory writers.
    analysis_writers = {
        "opensweep_platform_upsert_analysis",
        "opensweep_platform_set_analysis_section",
        "opensweep_platform_add_analysis_note",
        "opensweep_platform_ask_question",
    }
    missing_analysis = analysis_writers - actual
    assert not missing_analysis, f"analysis tools missing: {missing_analysis}"
    writers = writers | analysis_writers
    # Delivery ledger tools (PLATFORM_V2_DESIGN.md §11) — they write OpenSweep
    # ledger STATE (resolutions, verdicts, waiver requests), never the source
    # repository. Waive/blocking-override deliberately absent: human-API only.
    delivery = {
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
        # Tickets (§15 Phase 2) — propose (backlog-only, agent-proposal), refine
        # CONTENT in place (never status — see the transition guard below), + read.
        "opensweep_platform_create_ticket",
        "opensweep_platform_update_ticket",
        "opensweep_platform_get_ticket",
        "opensweep_platform_list_tickets",
        # Grouping — writes a TicketGroupProposal (OpenSweep state only); the
        # parent ticket is only materialized by a human approving the
        # proposal, so agents can never apply their own groupings.
        "opensweep_platform_propose_ticket_group",
        # Comments — the human↔agent conversation on any data item. Reading
        # shows human instructions; add_comment writes OpenSweep STATE only (a
        # thread reply attributed to the run), never the source repository.
        "opensweep_platform_list_comments",
        "opensweep_platform_add_comment",
        # Threads (unified dev flow) — plan DRAFTS (approval is human-only,
        # like Gate 1), the ready-for-review signal (a Thread flag only; the
        # platform's workflow booleans decide whether anything dispatches),
        # and structured user questions. All write OpenSweep thread STATE
        # only, never the source repository.
        "opensweep_platform_submit_thread_plan",
        "opensweep_platform_submit_for_review",
        "opensweep_platform_ask_user",
    }
    missing_delivery = delivery - actual
    assert not missing_delivery, f"delivery tools missing: {missing_delivery}"
    # Open-web READ tools: these are READ_TOOLS members mounted with
    # non-read_ operation ids (opensweep_platform_web_search /
    # opensweep_platform_fetch_url), so the opensweep_platform_read_* prefix rule
    # below cannot vouch for them. They query the open internet only
    # (SSRF-guarded) and never mutate OpenSweep state or the source repository.
    web_read_tools = {
        "opensweep_platform_web_search",
        "opensweep_platform_fetch_url",
    }
    missing_web_read = web_read_tools - actual
    assert not missing_web_read, f"web read tools missing: {missing_web_read}"
    unsafe = {
        name
        for name in actual
        if name not in writers
        and name not in delivery
        and name not in web_read_tools
        and not name.startswith("opensweep_platform_read_")
    }
    assert not unsafe, f"unrecognized platform-tool operations (potentially unsafe): {unsafe}"


def test_mount_forwards_opensweep_auth_headers(monkeypatch):
    """Tool invocations re-enter the ASGI app through TokenAuthMiddleware, so
    fastapi-mcp must forward the osrt_ auth headers into each internal call —
    its default allowlist is ['authorization'] only (regression: the MCP
    handshake authenticated fine but every tool call 401'd)."""
    import fastapi_mcp
    from fastapi import FastAPI

    import mcp_app

    captured = {}

    class _FakeMCP:
        def __init__(self, app, **kwargs):
            captured.update(kwargs)

        def mount(self, mount_path=""):
            pass

    monkeypatch.setattr(fastapi_mcp, "FastApiMCP", _FakeMCP)
    mcp_app.mount_mcp(FastAPI())
    forwarded = {h.lower() for h in captured.get("headers", [])}
    assert {"x-opensweep-auth", "x-opensweep-run-uid"} <= forwarded


def test_agent_waive_and_override_stay_off_the_tool_surface():
    """§11 role gating: agents may REQUEST a waiver but never waive or flip
    blocking overrides — those verbs must not exist on the executor mount."""
    actual = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)
    forbidden = {
        "opensweep_platform_waive_resolution",
        "opensweep_platform_set_blocking_override",
        "opensweep_waive_resolution",
        "opensweep_set_blocking_override",
    }
    assert not (forbidden & actual)


def test_ticket_transitions_stay_off_the_tool_surface():
    """Gate 1 (backlog → todo) is human-only (§2, §15 Phase 2): agents may
    propose tickets and refine their CONTENT, but must never MOVE a ticket's
    status. opensweep_platform_create_ticket forces status=backlog/agent-proposal;
    opensweep_platform_update_ticket refines fields but cannot touch status."""
    actual = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)
    forbidden = {
        "opensweep_platform_transition_ticket",
        "opensweep_platform_ticket_transition",
        "opensweep_platform_approve_ticket",
        "opensweep_platform_delete_ticket",
        "opensweep_ticket_transition",
        "opensweep_ticket_delete",
    }
    assert not (forbidden & actual)
    # Catch-all: any mounted ticket tool that can move status/approve/delete is
    # a Gate-1 leak. Content refinement ("update") is allowed — see below.
    movers = {
        name
        for name in actual
        if "ticket" in name
        and any(verb in name for verb in ("transition", "status", "approve", "delete"))
    }
    assert not movers, f"ticket status-moving tools leaked onto the executor mount: {movers}"

    # The refine tool is only safe because it CANNOT patch status: the
    # executor-facing request model has no `status` field, so Gate 1 stays
    # human-only even though the tool is exposed. Guard that invariant here.
    from api.v1.platform_tools_tickets import PlatformUpdateTicketRequest

    assert "status" not in PlatformUpdateTicketRequest.model_fields, (
        "opensweep_platform_update_ticket must not let agents move ticket status "
        "(Gate 1 is human-only)"
    )
