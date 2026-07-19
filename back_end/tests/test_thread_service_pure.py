"""ThreadService pure logic: DTO mapping + active-thread guard predicate."""

from types import SimpleNamespace

from domains.threads.services.thread_service import (
    has_active_thread,
    thread_to_dto,
)


def _thread(**over):
    base = dict(
        uid="th-1",
        repository_uid="r-1",
        subject_ticket_uid="t-1",
        phase="refining",
        plan_state="none",
        plan_text="",
        branch="",
        pr_uid="",
        ready_for_review=False,
        active_run_uid="",
        run_uids=[],
        events=[],
        created_by="u-1",
        created_at=None,
        updated_at=None,
        plan_approved_by="",
        plan_approved_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_dto_mapping_roundtrip():
    dto = thread_to_dto(_thread())
    assert dto.uid == "th-1" and dto.phase == "refining"


def test_has_active_thread_true_for_non_terminal():
    assert has_active_thread([_thread(phase="refining")])
    assert has_active_thread([_thread(phase="in_review")])


def test_has_active_thread_false_for_terminal_only():
    assert not has_active_thread([_thread(phase="done"), _thread(phase="abandoned")])
    assert not has_active_thread([])
