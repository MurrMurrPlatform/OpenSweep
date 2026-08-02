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
    "opensweep_list_agents", "opensweep_get_agent",
    "opensweep_list_scheduled_agents", "opensweep_get_scheduled_agent", "opensweep_create_scheduled_agent",
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


# ── The allowlists must describe REALITY ─────────────────────────────────────
#
# Every other test in this file compares a hardcoded set against
# OPENSWEEP_*_OPERATIONS — i.e. one list against another list. That can prove
# the allowlist SAYS the right thing, never that the names RESOLVE. When an
# operation_id is renamed and the allowlist is not, FastApiMCP's
# `include_operations` silently drops the entry: the mount succeeds, the tool
# vanishes, and the agent reports a tool it cannot find.
#
# That is exactly how `opensweep_platform_propose_epic` was unreachable — the
# allowlist still named the pre-rename `opensweep_platform_propose_ticket_group`.
# These two tests are the only ones here that consult the real OpenAPI schema,
# so they are the only ones that can catch the next rename.


def _real_operation_ids() -> set[str]:
    from app import app

    return {
        op["operationId"]
        for methods in app.openapi().get("paths", {}).values()
        for op in methods.values()
        if isinstance(op, dict) and op.get("operationId")
    }


def test_every_allowlisted_operation_resolves_to_a_real_route():
    real = _real_operation_ids()
    for label, listed in (
        ("external", OPENSWEEP_MCP_OPERATIONS),
        ("platform-tool", OPENSWEEP_PLATFORM_TOOL_OPERATIONS),
    ):
        missing = sorted(set(listed) - real)
        assert not missing, (
            f"{label} allowlist names operations that no route registers: {missing}. "
            "FastApiMCP drops these silently, so the tools are simply absent from "
            "the surface. Fix the name in mcp_app.py to match the route's "
            "operation_id (this is what a rename looks like)."
        )


def test_every_platform_route_is_exposed_to_executors():
    """The converse: a platform tool nobody allowlisted is a tool no run can call.

    `opensweep_platform_*` is by definition the executor-facing surface, so a
    route carrying that prefix and missing from the allowlist is a wiring
    mistake, not a deliberate omission. If one ever IS deliberate, add it to
    DELIBERATELY_UNEXPOSED with a comment saying why.
    """
    DELIBERATELY_UNEXPOSED: set[str] = set()

    unexposed = sorted(
        op
        for op in _real_operation_ids()
        if op.startswith("opensweep_platform_")
        and op not in set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)
        and op not in DELIBERATELY_UNEXPOSED
    )
    assert not unexposed, (
        f"platform routes exist but no run can reach them: {unexposed}. "
        "Add them to OPENSWEEP_PLATFORM_TOOL_OPERATIONS in mcp_app.py."
    )


def test_every_advertised_write_tool_is_reachable_over_mcp():
    """A tool we PROMISE agents in the prompt must exist on the MCP surface.

    The two tests above both start from the set of registered routes or the
    allowlist, so a tool that appears in neither is invisible to them — it
    contributes to no difference set. That is exactly how `confirm_area_current`
    shipped advertised-but-unreachable: it was in `dispatcher._TOOLS` (so the
    envelope transport worked) and in `PLATFORM_WRITE_TOOLS` (so every prompt
    named it), with no route and no allowlist entry, which meant claude_code and
    codex runs were told to call a tool that did not exist for them.

    This closes the pairing: advertised ⇒ reachable.
    """
    from domains.executors import prompt_kit

    # complete_run is reached through the same mount under its own name; every
    # other advertised write tool maps 1:1 onto opensweep_platform_<name>.
    advertised = set(prompt_kit.PLATFORM_WRITE_TOOLS)
    allowlisted = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)
    unreachable = sorted(
        name for name in advertised
        if f"opensweep_platform_{name}" not in allowlisted
    )
    assert not unreachable, (
        f"prompt_kit advertises tools with no MCP surface: {unreachable}. "
        "Every name in PLATFORM_WRITE_TOOLS is rendered into the executor "
        "prompts and promised as mcp__opensweep-platform__opensweep_platform_"
        "<name>; add a route in api/v1/platform_tools.py and the operation to "
        "OPENSWEEP_PLATFORM_TOOL_OPERATIONS in mcp_app.py."
    )


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
        "opensweep_platform_propose_area_edit",
        "opensweep_platform_confirm_doc_current",
        "opensweep_platform_confirm_area_current",
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
        # CONTENT in place (never status — see the transition guard below), read,
        # and move status. The transition tool writes OpenSweep ticket STATE
        # only (never the source repo) and is refused at the handler unless the
        # repo opted into agent_autonomy, so Gate 1 stays human-only by default.
        "opensweep_platform_create_ticket",
        "opensweep_platform_update_ticket",
        "opensweep_platform_transition_ticket",
        "opensweep_platform_get_ticket",
        "opensweep_platform_list_tickets",
        # Epics — writes an EpicProposal (OpenSweep state only). Materializing the
        # parent (approve) is human-only by default and refused at the handler
        # unless the repo opted into agent_autonomy.
        "opensweep_platform_propose_epic",
        "opensweep_platform_approve_epic",
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
        # The counterweight to ask_user under autonomy=assume: records a
        # decision the agent made instead of asking. Appends to
        # Ticket.assumptions — OpenSweep state only, never the source
        # repository — and the recorded value is surfaced to the reviewer
        # rather than acted on, so it cannot change what ships.
        "opensweep_platform_record_assumption",
        # Batch triage policy question: appends to Run.questions (OpenSweep
        # state only) and returns immediately — it cannot change what ships.
        "opensweep_platform_ask_policy_question",
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


def test_ticket_transitions_are_autonomy_gated_not_free():
    """Gate 1 (backlog → todo) is human-only BY DEFAULT (§2, §15 Phase 2). A
    dedicated transition tool exists, but it is refused at the handler unless the
    repo opted into agent_autonomy — so agents can never move their own tickets
    unless an operator explicitly allowed it. Delete is never exposed, and the
    content-refine tool still cannot touch status."""
    actual = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)

    # Deletion is never an agent verb, on any surface.
    forbidden = {
        "opensweep_platform_delete_ticket",
        "opensweep_ticket_delete",
    }
    assert not (forbidden & actual), f"forbidden ticket tools leaked: {forbidden & actual}"

    # The transition tool IS exposed (autonomy-gated), but the refine tool still
    # must not be able to patch status — status moves ONLY through the gated
    # transition tool, never as a silent side effect of a content refine.
    from api.v1.platform_tools_tickets import PlatformUpdateTicketRequest

    assert "status" not in PlatformUpdateTicketRequest.model_fields, (
        "opensweep_platform_update_ticket must not let agents move ticket status "
        "(status moves only through the autonomy-gated transition tool)"
    )

    # The transition + epic-approve handlers must consult the per-repo autonomy
    # gate — a transition tool that skipped it would make Gate 1 free-for-all.
    import inspect

    from api.v1 import platform_tools_tickets as ptt

    for fn in (ptt.platform_transition_ticket, ptt.platform_approve_epic):
        src = inspect.getsource(fn)
        assert "agent_autonomy_enabled" in src, (
            f"{fn.__name__} must gate on agent_autonomy_enabled (Gate 1 stays "
            "human-only unless the repo opted in)"
        )
