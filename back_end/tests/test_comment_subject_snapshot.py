"""subject_snapshot — the per-type prompt block rendered on every comment run.

`build_opensweep_comment_intent` and every briefing that snapshots a data
item lean on this function; a change of field name breaks every run silently
because the intent still renders (just without the removed line). Cover the
per-type shape here so a rename shows up as a test failure.
"""

from types import SimpleNamespace

from domains.comments.schemas import CommentSubjectType
from domains.comments.subjects import subject_snapshot


def _snapshot(kind: CommentSubjectType, subject) -> str:
    return subject_snapshot(kind, subject)


def test_common_header_is_present_for_every_type():
    """The type-agnostic header (Type/uid/Title) prefixes every snapshot;
    downstream prompts anchor on those three lines."""
    subject = SimpleNamespace(
        # union of every attribute any per-type branch reads
        uid="x1",
        title="A",
        kind="k",
        severity="high",
        status="open",
        description="d",
        priority="medium",
        acceptance_criteria=[],
        github_number=1,
        url="u",
        state="open",
        draft=False,
        head_ref="h",
        base_ref="m",
        category="cat",
        summary="s",
        relevance="r",
        playbook="pb",
        trigger="t",
        slug="sg",
    )
    for kind in CommentSubjectType:
        snap = _snapshot(kind, subject)
        assert f"Type: {kind.value}" in snap
        assert "uid: x1" in snap
        assert "Title: A" in snap


def test_missing_title_falls_back_to_untitled():
    """A finding with no title still snapshots — the rendered '(untitled)' is
    the signal that the finding needs work, not the placeholder for a crash."""
    snap = _snapshot(
        CommentSubjectType.FINDING,
        SimpleNamespace(
            uid="f1",
            title="",
            kind="bug",
            severity="high",
            status="open",
            description="d",
        ),
    )
    assert "Title: (untitled)" in snap


def test_finding_snapshot_carries_triage_facets():
    snap = _snapshot(
        CommentSubjectType.FINDING,
        SimpleNamespace(
            uid="f1",
            title="Race in cache",
            kind="bug",
            severity="high",
            status="acknowledged",
            description="The rollback path skips invalidate.",
        ),
    )
    assert "Kind: bug" in snap
    assert "Severity: high" in snap
    assert "Status: acknowledged" in snap
    assert "The rollback path skips invalidate." in snap


def test_ticket_snapshot_renders_acceptance_criteria_bullets():
    snap = _snapshot(
        CommentSubjectType.TICKET,
        SimpleNamespace(
            uid="t1",
            title="Fix login",
            status="todo",
            priority="high",
            description="Users see intermittent 401s.",
            acceptance_criteria=["logins succeed", "no 401 with valid cookie"],
        ),
    )
    assert "Status: todo" in snap
    assert "Priority: high" in snap
    assert "Users see intermittent 401s." in snap
    assert "  - logins succeed" in snap
    assert "  - no 401 with valid cookie" in snap


def test_ticket_snapshot_empty_acceptance_reads_as_none():
    snap = _snapshot(
        CommentSubjectType.TICKET,
        SimpleNamespace(
            uid="t1",
            title="Fix login",
            status="backlog",
            priority="medium",
            description="d",
            acceptance_criteria=[],
        ),
    )
    assert "  - (none)" in snap


def test_pull_request_snapshot_shows_github_number_and_branches():
    snap = _snapshot(
        CommentSubjectType.PULL_REQUEST,
        SimpleNamespace(
            uid="pr1",
            title="Event messaging",
            github_number=72,
            url="https://gh/o/r/pull/72",
            state="open",
            draft=False,
            head_ref="feat/event",
            base_ref="main",
        ),
    )
    assert "GitHub: #72 (https://gh/o/r/pull/72)" in snap
    assert "State: open (draft=False)" in snap
    assert "Branch: feat/event → main" in snap


def test_news_item_snapshot_shows_category_and_url_and_summary():
    snap = _snapshot(
        CommentSubjectType.NEWS_ITEM,
        SimpleNamespace(
            uid="n1",
            title="CVE in dep",
            category="security",
            url="https://cve.example/1",
            summary="RCE in libfoo <2.0",
            relevance="high",
        ),
    )
    assert "Category: security" in snap
    assert "URL: https://cve.example/1" in snap
    assert "Summary: RCE in libfoo <2.0" in snap
    assert "Relevance: high" in snap


def test_run_snapshot_shows_playbook_and_status():
    snap = _snapshot(
        CommentSubjectType.RUN,
        SimpleNamespace(uid="r1", title="Refine", playbook="refine", status="running"),
    )
    assert "Playbook: refine" in snap
    assert "Status: running" in snap


def test_scheduled_agent_snapshot_shows_trigger():
    snap = _snapshot(
        CommentSubjectType.SCHEDULED_AGENT,
        SimpleNamespace(uid="sa1", title="Nightly audit", trigger="on-schedule"),
    )
    assert "Trigger: on-schedule" in snap


def test_doc_snapshot_shows_slug_and_summary():
    snap = _snapshot(
        CommentSubjectType.DOC,
        SimpleNamespace(
            uid="d1",
            title="Runs domain",
            slug="backend/domains/runs",
            summary="Run lifecycle and dispatch",
        ),
    )
    assert "Slug: backend/domains/runs" in snap
    assert "Summary: Run lifecycle and dispatch" in snap
