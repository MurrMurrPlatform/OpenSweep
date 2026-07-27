"""Implement/fix run intent contracts — the write-path prompts must pin the
ticket AC / finding evidence, forbid pushing, and carry the denylist (§6)."""

from domains.delivery.models import DEFAULT_PATH_DENYLIST, PullRequest
from domains.delivery.services.fix_run_service import build_fix_intent
from domains.delivery.services.implement_run_service import (
    branch_name_for_ticket,
    build_implement_intent,
    slug,
)
from domains.tickets.models import Ticket


def _ticket() -> Ticket:
    return Ticket(
        uid="abcd1234deadbeefabcd1234deadbeef",
        repository_uid="repo1",
        title="Add retry logic to webhook delivery",
        description="Webhook deliveries fail permanently on transient errors.",
        acceptance_criteria=[
            "Failed deliveries retry up to 3 times with backoff",
            "A permanently failed delivery is audited",
        ],
        status="todo",
    )


def _pr() -> PullRequest:
    return PullRequest(
        uid="pr1",
        repository_uid="repo1",
        github_number=91,
        pr_key="repo1:91",
        title="Add retry logic",
        head_ref="opensweep/abcd1234-add-retry-logic-to-webhook",
        base_ref="main",
        head_sha="c0ffee1234deadbeefc0ffee1234deadbeefc0ff",
    )


# ── Branch naming ────────────────────────────────────────────────────────────


def test_slug_is_branch_safe_and_capped():
    assert slug("Add retry logic to webhook delivery!!") == "add-retry-logic-to-webhook-del"
    assert len(slug("x" * 100)) <= 30
    assert slug("///???") == "work"
    assert slug("Ünïcode & Symbols") == "n-code-symbols"


def test_branch_name_uses_ticket_uid_prefix_and_slug():
    name = branch_name_for_ticket(_ticket())
    assert name.startswith("opensweep/abcd1234-")
    assert name == "opensweep/abcd1234-add-retry-logic-to-webhook-del"
    # never a protected name
    assert name not in {"main", "master", "develop"}


# ── Implement intent ─────────────────────────────────────────────────────────


def test_implement_intent_pins_acceptance_criteria_and_ticket_uid():
    intent = build_implement_intent(
        _ticket(),
        work_branch="opensweep/abcd1234-add-retry",
        base_branch="main",
        denylist=DEFAULT_PATH_DENYLIST,
    )
    assert "Failed deliveries retry up to 3 times with backoff" in intent
    assert "A permanently failed delivery is audited" in intent
    assert "OpenSweep-Ticket: abcd1234deadbeefabcd1234deadbeef" in intent


def test_implement_intent_forbids_pushing_and_lists_denylist():
    intent = build_implement_intent(
        _ticket(),
        work_branch="opensweep/abcd1234-add-retry",
        base_branch="main",
        denylist=DEFAULT_PATH_DENYLIST,
    )
    assert "DO NOT push" in intent
    assert "platform validates and pushes" in intent
    for pattern in DEFAULT_PATH_DENYLIST:
        assert pattern in intent, f"denylist pattern {pattern!r} must be listed"


def test_implement_intent_states_branch_is_checked_out_and_tests_and_complete_run():
    intent = build_implement_intent(
        _ticket(),
        work_branch="opensweep/abcd1234-add-retry",
        base_branch="main",
        denylist=[],
    )
    assert "`opensweep/abcd1234-add-retry` is already checked out" in intent
    assert "pyproject.toml" in intent and "package.json" in intent
    assert "complete_run" in intent
    assert "MINIMAL" in intent


def test_implement_intent_mandates_doc_and_memory_upkeep():
    intent = build_implement_intent(
        _ticket(),
        work_branch="opensweep/abcd1234-add-retry",
        base_branch="main",
        denylist=[],
    )
    # Pull the right docs before, write them back after (KNOWLEDGE_V3 §9).
    assert "opensweep_platform_read_doc" in intent
    assert "opensweep_platform_propose_doc_edit" in intent
    assert "opensweep_platform_confirm_doc_current" in intent
    assert "opensweep_platform_write_memory" in intent


def test_implement_intent_continuation_and_addendum():
    intent = build_implement_intent(
        _ticket(),
        work_branch="opensweep/abcd1234-add-retry",
        base_branch="main",
        denylist=[],
        continuation=True,
        addendum="## Extra\nappended details here",
    )
    assert "already contains earlier work" in intent
    assert intent.rstrip().endswith("appended details here")


# ── Fix intent ───────────────────────────────────────────────────────────────


def _findings() -> list[dict]:
    return [
        {
            "resolution_uid": "res-1",
            "finding_uid": "f-1",
            "title": "Race condition in delivery queue",
            "severity": "high",
            "concern": "correctness",
            "blocking": True,
            "why_it_matters": "Duplicate sends under load.",
            "suggested_fix": "Lock the queue row before dispatch.",
            "evidence": {"path": "src/queue.py", "line": 42},
            "affected_paths": ["src/queue.py"],
        }
    ]


def test_fix_intent_carries_finding_evidence_and_resolution_uids():
    intent = build_fix_intent(_pr(), _findings(), DEFAULT_PATH_DENYLIST)
    assert "Race condition in delivery queue" in intent
    assert "`res-1`" in intent
    assert "Lock the queue row before dispatch." in intent
    assert "src/queue.py" in intent


def test_fix_intent_mandates_attach_fix_tool_and_forbids_push():
    intent = build_fix_intent(_pr(), _findings(), DEFAULT_PATH_DENYLIST)
    assert "opensweep_platform_attach_fix" in intent
    assert "DO NOT push" in intent
    assert "platform validates and pushes" in intent
    for pattern in DEFAULT_PATH_DENYLIST:
        assert pattern in intent


def test_fix_intent_stays_on_the_existing_head_branch():
    intent = build_fix_intent(_pr(), _findings(), [])
    assert "`opensweep/abcd1234-add-retry-logic-to-webhook` is already checked out" in intent
    assert "Never switch branches" in intent
    assert "complete_run" in intent


def test_fix_intent_mandates_doc_and_memory_upkeep():
    intent = build_fix_intent(_pr(), _findings(), DEFAULT_PATH_DENYLIST)
    assert "opensweep_platform_read_doc" in intent
    assert "opensweep_platform_propose_doc_edit" in intent
    assert "opensweep_platform_confirm_doc_current" in intent
    assert "opensweep_platform_write_memory" in intent
