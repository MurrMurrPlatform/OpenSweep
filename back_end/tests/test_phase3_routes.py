"""Phase 3 write-path surface — routes exist, vocabulary extended, and the
GITHUB_TOKEN stays out of the agent process (§6, §13)."""

import os
from unittest import mock

from app import app
from domains.executors.mcp_bridge import claude_env
from domains.runs.schemas import ExecutionMode


def _openapi_operation_ids() -> set[str]:
    schema = app.openapi()
    ops = set()
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if isinstance(op, dict) and op.get("operationId"):
                ops.add(op["operationId"])
    return ops


def test_phase3_routes_are_mounted():
    ops = _openapi_operation_ids()
    for op_id in ("opensweep_trigger_fix_run", "opensweep_trigger_ratchet", "opensweep_ticket_implement"):
        assert op_id in ops, f"missing Phase 3 operation {op_id}"


def test_phase3_route_paths():
    # app.routes holds lazy _IncludedRouter entries until openapi() renders,
    # so assert against the schema paths.
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/delivery/pull-requests/{uid}/fix" in paths
    assert "/api/v1/findings/ratchet" in paths
    assert "/api/v1/tickets/{uid}/implement" in paths


def test_merge_policy_dto_exposes_path_denylist():
    schema = app.openapi()
    props = schema["components"]["schemas"]["MergePolicyDTO"]["properties"]
    assert "path_denylist" in props


def test_code_changes_map_to_the_implement_playbook():
    from domains.agents.services.registry import PRODUCES_TO_PLAYBOOK
    from domains.agents.services.seed_agent_bases import _AGENT_BASES

    assert PRODUCES_TO_PLAYBOOK["code-changes"] == "implement"
    # The write agent keeps the never-push rule in its seeded instructions.
    assert "never push" in _AGENT_BASES["implement"]["body"].lower()


def test_execution_mode_gains_implement_and_keeps_analyze_only():
    assert ExecutionMode.ANALYZE_ONLY.value == "analyze_only"
    assert ExecutionMode.IMPLEMENT.value == "implement"


def test_executor_env_never_receives_the_github_token():
    """The agent env is an explicit ALLOWLIST: no platform secret — known or
    future — may leak into the `claude --permission-mode bypassPermissions`
    subprocess. Strip-list behavior (os.environ minus known-bad names) is a
    regression."""
    with mock.patch.dict(
        os.environ,
        {
            "GITHUB_TOKEN": "ghp_secret",
            "GH_TOKEN": "gho_secret",
            "GITHUB_PAT": "pat_secret",
            "NEO4J_PASSWORD": "neo4j_secret",
            "OPENSWEEP_AUTH_TOKEN": "opensweep_secret",
            "GITHUB_WEBHOOK_SECRET": "hook_secret",
            "SOME_FUTURE_SECRET": "surprise",
            "NPM_CONFIG_CACHE": "/tmp/npm-cache",
        },
    ):
        env = claude_env(run_uid="run123", oauth_token="claude-oauth")

    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PAT",
        "NEO4J_PASSWORD",
        "OPENSWEEP_AUTH_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "SOME_FUTURE_SECRET",  # allowlist ⇒ unknown vars never pass through
    ):
        assert name not in env, f"{name} leaked into the agent env"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth"
    assert env["OPENSWEEP_RUN_UID"] == "run123"
    assert env["IS_SANDBOX"] == "1"
    # Allowlisted vars still pass through (stdio MCP servers need node/npm bits).
    assert env["NPM_CONFIG_CACHE"] == "/tmp/npm-cache"
    assert env.get("PATH") == os.environ.get("PATH")
    # MCP startup budget travels with every claude invocation.
    assert env["MCP_TIMEOUT"] == "60000"


def test_platform_mcp_url_uses_configured_backend(monkeypatch):
    from config import settings
    from domains.executors import mcp_bridge

    monkeypatch.setattr(settings, "OPENSWEEP_BACKEND_INTERNAL_URL", "http://backend.internal:9000/")
    assert mcp_bridge.platform_mcp_url() == "http://backend.internal:9000/mcp/platform"


def test_probe_platform_mcp_reports_unreachable_backend(monkeypatch):
    """Transport failures surface as a diagnostic string, never an exception —
    the adapter turns this into a clear FAILED run before spawning the CLI."""
    import asyncio

    from config import settings
    from domains.executors import mcp_bridge

    # Port 9 (discard) is never serving HTTP; connection is refused instantly.
    monkeypatch.setattr(settings, "OPENSWEEP_BACKEND_INTERNAL_URL", "http://127.0.0.1:9")
    detail = asyncio.run(mcp_bridge.probe_platform_mcp(timeout_seconds=0.5))
    assert detail


def test_codex_turn_env_is_allowlisted_too():
    """Same allowlist rule for the codex executor's turn env."""
    from domains.runs.services.turn_cli import codex_turn_env

    class _Provider:
        credential_secret = ""
        kind = "codex_subscription"
        api_key_env = ""

    with mock.patch.dict(
        os.environ,
        {"NEO4J_PASSWORD": "neo4j_secret", "OPENSWEEP_AUTH_TOKEN": "opensweep_secret"},
    ):
        env = codex_turn_env(_Provider(), run_uid="run-abc")
    assert "NEO4J_PASSWORD" not in env
    assert "OPENSWEEP_AUTH_TOKEN" not in env
    assert env["OPENSWEEP_RUN_UID"] == "run-abc"
    assert env["IS_SANDBOX"] == "1"


def test_write_intents_never_mention_the_token():
    from domains.delivery.models import DEFAULT_PATH_DENYLIST, PullRequest
    from domains.delivery.services.fix_run_service import build_fix_intent
    from domains.delivery.services.implement_run_service import build_implement_intent
    from domains.tickets.models import Ticket

    ticket = Ticket(uid="a" * 32, repository_uid="r", title="T", status="todo")
    pr = PullRequest(
        uid="p", repository_uid="r", github_number=1, pr_key="r:1", head_ref="opensweep/x"
    )
    implement = build_implement_intent(
        ticket, work_branch="opensweep/x", base_branch="main", denylist=DEFAULT_PATH_DENYLIST
    )
    fix = build_fix_intent(pr, [], DEFAULT_PATH_DENYLIST)
    for intent in (implement, fix):
        assert "GITHUB_TOKEN" not in intent
        assert "ghp_" not in intent
