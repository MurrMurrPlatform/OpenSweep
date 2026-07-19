"""Auto-audit surface (§F) — signatures, system agent key, scanner predicate.

run_auto_audit itself touches Neo4j + dispatch; its selection math is
covered purely in test_audit_selection.py.
"""

import inspect

from domains.agents.services.registry import AGENT_KEYS
from domains.agents.services.schedule_scanner import should_auto_audit
from domains.agents.services.seed_agent_bases import _AGENT_BASES
from domains.runs.services.sweep import AuditResult, run_auto_audit


def test_audit_stale_system_agent_exists():
    assert "audit-stale" in AGENT_KEYS
    assert "stale" in _AGENT_BASES["audit-stale"]["description"].lower()


def test_run_auto_audit_signature():
    params = inspect.signature(run_auto_audit).parameters
    assert {"repository_uid", "limit", "triggered_by", "agent_uid", "custom_intent"} <= set(params)
    assert params["limit"].default == 3


def test_audit_result_carries_selection_provenance():
    result = AuditResult(repository_uid="r1", doc_count=0)
    assert result.selected == []


def test_scanner_predicate_gates_on_agent_key_and_dial():
    assert should_auto_audit("audit-stale", "ask-before-run")
    assert should_auto_audit("audit-stale", "auto-run-any")
    # disabled is the kill-safety even with a cron set.
    assert not should_auto_audit("audit-stale", "disabled")
    # Other agent keys keep the classic single-run dispatch path.
    assert not should_auto_audit("ask", "auto-run-any")


def test_seeded_scheduled_agent_is_inert_until_a_cron_is_set():
    from domains.agents.services.scheduled_agent_service import (
        AUDIT_STALE_TITLE,
        seed_audit_stale,
    )

    assert AUDIT_STALE_TITLE == "Audit stale code"
    # Contract documented in the seeder: trigger="" (cron is the opt-in).
    src = inspect.getsource(seed_audit_stale)
    assert 'trigger=""' in src
