"""Delivery-domain DTO mapping — pure functions on PullRequest/Verdict nodes.

`pull_request_to_dto` and `verdict_to_dto` normalize field defaults out of the
Neo4j nodes for the API surface. The mapping has to survive:

- pre-Phase-3 rows without the newer fields (falsy → defaults, not exceptions),
- a persisted `convergence` snapshot that no longer matches the schema
  (malformed → None, never a 500), and
- verdicts persisted with the older empty-executor default (empty → "manual").
"""

from datetime import UTC, datetime

from domains.delivery.models import PullRequest, Verdict
from domains.delivery.schemas import (
    CIState,
    ConvergenceState,
    PRState,
    VerdictResult,
)
from domains.delivery.services.pull_request_service import (
    pull_request_to_dto,
    verdict_to_dto,
)

HEAD = "a" * 40


def _pr(**over) -> PullRequest:
    base = dict(
        uid="pr1",
        repository_uid="repo1",
        github_number=7,
        pr_key="repo1:7",
        title="t",
        head_sha=HEAD,
        head_ref="feat/x",
        base_ref="main",
    )
    base.update(over)
    return PullRequest(**base)


def _verdict(**over) -> Verdict:
    base = dict(
        uid="v1",
        pull_request_uid="pr1",
        repository_uid="repo1",
        sha=HEAD,
        result="approve",
    )
    base.update(over)
    return Verdict(**base)


# ── PullRequest → DTO ────────────────────────────────────────────────────────


def test_pull_request_to_dto_maps_the_core_shape():
    dto = pull_request_to_dto(_pr(author="alice", url="https://gh/p/7"))
    assert dto.uid == "pr1"
    assert dto.repository_uid == "repo1"
    assert dto.github_number == 7
    assert dto.author == "alice"
    assert dto.url == "https://gh/p/7"
    assert dto.state == PRState.OPEN  # default
    assert dto.ci_state == CIState.EMPTY  # default
    assert dto.head_sha == HEAD
    assert dto.head_ref == "feat/x"
    assert dto.base_ref == "main"
    assert dto.base_is_default is True
    assert dto.converged is False
    assert dto.convergence is None
    assert dto.fix_rounds == 0
    assert dto.fix_rounds_exhausted is False


def test_pull_request_to_dto_survives_missing_optional_fields():
    """A pre-Phase-3 row lacks the newer text/URL fields; the DTO must not
    raise and must not surface Python's `None`."""
    pr = _pr(author=None, url=None, title=None, head_sha=None, head_ref=None, base_ref=None)
    dto = pull_request_to_dto(pr)
    assert dto.author == ""
    assert dto.url == ""
    assert dto.title == ""
    assert dto.head_sha == ""
    assert dto.head_ref == ""
    assert dto.base_ref == ""


def test_pull_request_to_dto_decodes_persisted_convergence():
    snapshot = ConvergenceState(
        converged=True, head_sha=HEAD, ci_state=CIState.GREEN
    ).model_dump(mode="json")
    dto = pull_request_to_dto(_pr(convergence=snapshot, converged=True))
    assert dto.converged is True
    assert dto.convergence is not None
    assert dto.convergence.converged is True
    assert dto.convergence.ci_state == CIState.GREEN


def test_pull_request_to_dto_drops_a_malformed_convergence_snapshot():
    """A schema change must not 500 the PR list. Malformed → None so callers
    keep working; the flag on the node still tells the truth."""
    dto = pull_request_to_dto(_pr(convergence={"not_a_field": True}, converged=False))
    assert dto.convergence is None
    assert dto.converged is False


def test_pull_request_to_dto_carries_ci_checks_verbatim():
    checks = [
        {"name": "ci", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "in_progress", "conclusion": None},
    ]
    dto = pull_request_to_dto(_pr(ci_checks=checks, ci_state="pending"))
    assert dto.ci_state == CIState.PENDING
    assert dto.ci_checks == checks


def test_pull_request_to_dto_coerces_integer_fields():
    """Neomodel returns ints, but callers upstream sometimes stash strings on a
    JSON round-trip; the DTO must coerce rather than propagate the type."""
    dto = pull_request_to_dto(_pr(fix_rounds="3"))
    assert dto.fix_rounds == 3


# ── Verdict → DTO ────────────────────────────────────────────────────────────


def test_verdict_to_dto_maps_the_core_shape():
    v = _verdict(
        finding_uids=["f1", "f2"],
        new_blocking_findings=2,
        source_run_uid="run-1",
        executor="claude",
        ac_results=[{"criterion": "does the thing", "result": "pass", "note": ""}],
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    dto = verdict_to_dto(v)
    assert dto.uid == "v1"
    assert dto.pull_request_uid == "pr1"
    assert dto.sha == HEAD
    assert dto.result == VerdictResult.APPROVE
    assert dto.new_blocking_findings == 2
    assert dto.finding_uids == ["f1", "f2"]
    assert dto.source_run_uid == "run-1"
    assert dto.executor == "claude"
    assert [ac.result for ac in dto.ac_results] == ["pass"]


def test_verdict_to_dto_defaults_executor_when_empty():
    """Older rows stored `executor=""` — the DTO surfaces "manual" so the UI
    can render a real string rather than nothing."""
    dto = verdict_to_dto(_verdict(executor=""))
    assert dto.executor == "manual"


def test_verdict_to_dto_defaults_verification_when_absent():
    dto = verdict_to_dto(_verdict())
    assert dto.verification_status == ""
    assert dto.verification_run_uid == ""


def test_verdict_to_dto_normalizes_blocking_count():
    """`new_blocking_findings` defaults to 0 on the node but None can appear on
    older data — the DTO must coerce, not propagate."""
    dto = verdict_to_dto(_verdict(new_blocking_findings=None))
    assert dto.new_blocking_findings == 0
