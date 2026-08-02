"""Rev2 thread playbook: vocabulary, phase-gated finalize, go/fix messages."""

import asyncio
from types import SimpleNamespace

from domains.runs.models import PLAYBOOKS as MODEL_PLAYBOOKS
from domains.runs.services.active_runs import WRITE_PLAYBOOKS as ACTIVE_WRITE
from domains.runs.services.playbooks import (
    CHECKED_PLAYBOOKS,
    PLAYBOOKS,
    WRITE_PLAYBOOKS,
)
from domains.delivery.services.write_gate import (
    NO_COMMITS_VIOLATION,
    is_only_no_commits,
)
from domains.threads.services.thread_run import (
    build_fix_message,
    build_go_message,
    finalize_thread_run,
)


def test_thread_playbook_is_registered_everywhere():
    assert "thread" in PLAYBOOKS
    assert "thread" in MODEL_PLAYBOOKS
    assert "thread" in WRITE_PLAYBOOKS
    assert "thread" in ACTIVE_WRITE
    assert "thread" in CHECKED_PLAYBOOKS


def test_the_playbook_vocabulary_has_exactly_one_definition():
    """The service set and the model set were two literals that drifted — the
    model's omitted `refine`, which lifecycle would happily dispatch."""
    assert PLAYBOOKS is MODEL_PLAYBOOKS
    assert "refine" in PLAYBOOKS


def test_quiet_gate_predicate():
    assert is_only_no_commits([NO_COMMITS_VIOLATION])
    assert not is_only_no_commits([])
    assert not is_only_no_commits([NO_COMMITS_VIOLATION, "denylist hit"])


def test_finalize_is_noop_without_thread_uid():
    run = SimpleNamespace(thread_uid="", uid="r-1")
    asyncio.run(finalize_thread_run(run))  # must not touch the DB / raise


def _ticket():
    return SimpleNamespace(uid="t-1", title="Fix labels")


def test_go_message_carries_plan_and_contract():
    go = build_go_message(
        ticket=_ticket(),
        plan_state="approved",
        plan_text="## Plan\n1. do X",
        work_branch="opensweep/t-1-fix",
        base_branch="main",
        denylist=[".github/**"],
        children=None,
    )
    assert go.startswith("GO —")
    assert "## Plan" in go and "1. do X" in go
    assert "opensweep/t-1-fix" in go and ".github/**" in go
    assert "OpenSweep-Ticket: t-1" in go
    assert "DO NOT push" in go
    assert "test_note" in go


def test_go_message_without_plan_still_instructs():
    go = build_go_message(
        ticket=_ticket(),
        plan_state="none",
        plan_text="",
        work_branch="b",
        base_branch="main",
        denylist=[],
        children=None,
    )
    assert "No plan was drafted" in go


def test_go_message_includes_group_batch():
    child = SimpleNamespace(
        uid="c-1", title="Sub", description="d", acceptance_criteria=["a"]
    )
    go = build_go_message(
        ticket=_ticket(),
        plan_state="none",
        plan_text="",
        work_branch="b",
        base_branch="main",
        denylist=[],
        children=[child],
    )
    assert "ALL subtickets" in go and "c-1" in go


def test_fix_message_lists_findings_with_resolutions():
    pr = SimpleNamespace(github_number=7)
    findings = [
        {
            "resolution_uid": "res-1",
            "title": "Null deref",
            "why_it_matters": "crashes on empty list",
            "suggested_fix": "guard the loop",
            "affected_paths": ["a.py"],
            "blocking": True,
        }
    ]
    msg = build_fix_message(pr, findings, fix_round=2, max_rounds=3)
    assert "fix round 2/3" in msg and "PR #7" in msg
    assert "Null deref" in msg and "[BLOCKING]" in msg and "res-1" in msg
    assert "attach_fix" in msg
