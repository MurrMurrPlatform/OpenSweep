"""filter_pending_for_subject — pure filter over comment-surface runs.

`pending_opensweep_runs` backs the "OpenSweep is thinking…" bubble that has
to survive a page reload — the filter must match on BOTH generic keys
(`subject_type`, `subject_uid`) even when the run's older typed key
(`finding_uid`, `ticket_uid`, …) is also on `target`, and never leak
another subject's run into the thread.
"""

from types import SimpleNamespace

from domains.comments.opensweep_mention import filter_pending_for_subject
from domains.comments.schemas import CommentSubjectType


def _run(**target) -> SimpleNamespace:
    return SimpleNamespace(uid="run-x", target=dict(target))


def test_matches_on_generic_subject_keys():
    """The generic keys are what makes the lookup type-agnostic — any comment
    thread finds its in-flight runs without knowing the typed field name."""
    run = _run(subject_type="ticket", subject_uid="t1")
    out = filter_pending_for_subject([run], CommentSubjectType.TICKET, "t1")
    assert out == [run]


def test_a_run_on_a_different_subject_is_dropped():
    a = _run(subject_type="ticket", subject_uid="t1")
    b = _run(subject_type="ticket", subject_uid="t2")
    assert filter_pending_for_subject([a, b], CommentSubjectType.TICKET, "t1") == [a]


def test_a_run_on_a_different_subject_type_is_dropped():
    """A finding thread must not surface a ticket-thread run and vice versa,
    even when the uids coincidentally match."""
    finding_run = _run(subject_type="finding", subject_uid="x1")
    ticket_run = _run(subject_type="ticket", subject_uid="x1")
    out = filter_pending_for_subject(
        [finding_run, ticket_run], CommentSubjectType.TICKET, "x1"
    )
    assert out == [ticket_run]


def test_a_run_without_a_target_is_dropped():
    """target is JSONProperty(default={}) but older rows may hold None."""
    silent = SimpleNamespace(uid="r0", target=None)
    matching = _run(subject_type="ticket", subject_uid="t1")
    out = filter_pending_for_subject(
        [silent, matching], CommentSubjectType.TICKET, "t1"
    )
    assert out == [matching]


def test_an_empty_input_is_an_empty_output():
    assert filter_pending_for_subject([], CommentSubjectType.TICKET, "t1") == []


def test_uses_the_enum_value_not_the_name():
    """`subject_type` on the target is the enum VALUE (e.g. "pull_request"),
    not the name — regressions here silently orphan the thinking bubble."""
    run = _run(subject_type="pull_request", subject_uid="pr1")
    out = filter_pending_for_subject([run], CommentSubjectType.PULL_REQUEST, "pr1")
    assert out == [run]
