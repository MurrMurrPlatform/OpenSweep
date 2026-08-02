"""Verdicts reach GitHub as a review, not just a commit status.

Before this, `create_commit_status` was the only thing the delivery loop wrote
to GitHub: a reviewer saw `opensweep/converged` green or red and none of the
reasoning. These cover the anchoring rules, since GitHub rejects an entire
review with 422 when any inline comment points outside the diff.
"""

import pytest

from domains.delivery.services.review_publisher import (
    build_review,
    parse_path_line,
    publish_verdict_review,
)

POLICY = {"default": "high", "per_tag": {"security": "medium"}}


def _finding(title, severity="high", paths=(), tags=()):
    return {
        "title": title,
        "severity": severity,
        "tags": list(tags),
        "affected_paths": list(paths),
        "description": "d",
        "why_it_matters": "w",
        "suggested_fix": "f",
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("src/a.py:12", ("src/a.py", 12)),
        ("src/a.py:12-30", ("src/a.py", 12)),
        ("src/a.py", ("src/a.py", None)),
        ("", ("", None)),
        ("weird:notanumber", ("weird:notanumber", None)),
    ],
)
def test_parse_path_line(raw, expected):
    assert parse_path_line(raw) == expected


def test_anchors_only_inside_the_diff():
    """A comment on an unchanged file would 422 the whole review."""
    findings = [
        _finding("in diff", paths=["src/a.py:10"]),
        _finding("outside diff", paths=["src/other.py:10"]),
    ]
    body, inline = build_review(
        verdict={"result": "request_changes", "new_blocking_findings": 2},
        findings=findings,
        changed_paths={"src/a.py"},
        blocking_policy=POLICY,
    )
    assert [c["path"] for c in inline] == ["src/a.py"]
    assert inline[0]["line"] == 10
    assert inline[0]["side"] == "RIGHT"
    # The un-anchored one is not lost — it moves into the body.
    assert "outside diff" in body
    assert "Not anchored to changed lines" in body


def test_finding_without_a_line_goes_to_the_body():
    body, inline = build_review(
        verdict={"result": "request_changes", "new_blocking_findings": 1},
        findings=[_finding("no line", paths=["src/a.py"])],
        changed_paths={"src/a.py"},
        blocking_policy=POLICY,
    )
    assert inline == []
    assert "no line" in body


def test_blocking_marker_uses_the_policy_not_the_claim():
    body, _ = build_review(
        verdict={"result": "request_changes", "new_blocking_findings": 0},
        findings=[_finding("medium security", severity="medium", tags=["security"])],
        changed_paths=set(),
        blocking_policy=POLICY,
    )
    assert "(blocking)" in body


def test_inline_comments_are_capped_and_overflow_summarized():
    findings = [_finding(f"f{i}", paths=[f"src/a.py:{i + 1}"]) for i in range(50)]
    body, inline = build_review(
        verdict={"result": "request_changes", "new_blocking_findings": 50},
        findings=findings,
        changed_paths={"src/a.py"},
        blocking_policy=POLICY,
    )
    assert len(inline) == 30
    assert "f45" in body  # overflow still visible


def test_acceptance_criteria_render():
    body, _ = build_review(
        verdict={
            "result": "approve",
            "new_blocking_findings": 0,
            "ac_results": [
                {"criterion": "does the thing", "result": "pass"},
                {"criterion": "handles errors", "result": "fail"},
            ],
        },
        findings=[],
        changed_paths=set(),
        blocking_policy=POLICY,
    )
    assert "Acceptance criteria" in body
    assert "✅ does the thing" in body
    assert "❌ handles errors" in body


# ── publish_verdict_review: failure isolation ────────────────────────────────


class _Repo:
    uid = "repo-1"
    github_owner = "acme"
    github_repo = "widgets"


class _PR:
    uid = "pr-1"
    github_number = 7
    state = "open"
    pr_key = "acme/widgets#7"
    repository_uid = "repo-1"


def test_pr_fake_matches_real_model():
    """The fake must expose only attributes the real PullRequest model has.

    publish_verdict_review read `pr.number` for four commits while the model
    only defines `github_number`; the fake carried a matching `number = 7`, so
    the tests were green while every real verdict raised AttributeError. This
    guard fails if the fake and the model drift again.
    """
    from domains.delivery.models import PullRequest

    real_attrs = set(dir(PullRequest))
    fake_attrs = {a for a in vars(_PR) if not a.startswith("_")}
    missing = fake_attrs - real_attrs
    assert not missing, f"_PR has attributes absent from PullRequest: {missing}"


class _Client:
    is_active = True

    def __init__(self, *, fail_with_comments=False, fail_always=False):
        self.fail_with_comments = fail_with_comments
        self.fail_always = fail_always
        self.calls = []

    async def list_pull_request_files(self, owner, repo, number):
        return [{"filename": "src/a.py"}]

    async def create_review(self, owner, repo, number, **kw):
        self.calls.append(kw)
        if self.fail_always:
            raise RuntimeError("502")
        if self.fail_with_comments and kw.get("comments"):
            raise RuntimeError("422 line not part of the diff")
        return {"id": 1}


class _Policy:
    blocking = POLICY


def _install(monkeypatch, client):
    monkeypatch.setattr(
        "infrastructure.git_providers.get_provider_client", lambda repo: client
    )

    async def _policy(_uid):
        return _Policy()

    monkeypatch.setattr(
        "domains.delivery.services.resolution_service.ensure_merge_policy", _policy
    )


async def test_falls_back_to_body_only_when_inline_comments_are_rejected(monkeypatch):
    """A 422 on one bad anchor must not lose the whole review."""
    client = _Client(fail_with_comments=True)
    _install(monkeypatch, client)

    ok = await publish_verdict_review(
        repo=_Repo(),
        pr=_PR(),
        verdict={"result": "request_changes", "sha": "abc", "new_blocking_findings": 1},
        findings=[_finding("boom", paths=["src/a.py:3"])],
    )

    assert ok is True
    assert len(client.calls) == 2
    assert client.calls[0].get("comments")  # first attempt had anchors
    assert not client.calls[1].get("comments")  # retry did not


async def test_github_outage_never_raises(monkeypatch):
    """The verdict is already persisted; GitHub failing must not undo it."""
    client = _Client(fail_always=True)
    _install(monkeypatch, client)

    ok = await publish_verdict_review(
        repo=_Repo(),
        pr=_PR(),
        verdict={"result": "approve", "sha": "abc", "new_blocking_findings": 0},
        findings=[],
    )

    assert ok is False


async def test_skips_closed_pull_requests(monkeypatch):
    client = _Client()
    _install(monkeypatch, client)

    class _Closed(_PR):
        state = "closed"

    ok = await publish_verdict_review(
        repo=_Repo(), pr=_Closed(), verdict={"result": "approve", "sha": "a"}, findings=[]
    )

    assert ok is False
    assert client.calls == []


async def test_never_uses_approve_or_request_changes_events(monkeypatch):
    """APPROVE/REQUEST_CHANGES would interact with branch protection."""
    client = _Client()
    _install(monkeypatch, client)

    await publish_verdict_review(
        repo=_Repo(),
        pr=_PR(),
        verdict={"result": "request_changes", "sha": "abc", "new_blocking_findings": 1},
        findings=[_finding("x", paths=["src/a.py:1"])],
    )

    assert client.calls[0]["event"] == "COMMENT"
