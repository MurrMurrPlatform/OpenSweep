"""Resolution/MergePolicy DTO mapping — pure functions on delivery nodes.

`merge_policy_to_dto` and `resolution_to_dto` translate the delivery ledger
onto the API surface. The two rules under test:

- The MergePolicy denylist has three meaningful states — `None` (pre-Phase-3
  node) resolves to the default set; explicit `[]` is a operator opt-out; any
  other list is passed through. Confusing the first two would either import
  legacy noise or silently disable a safety knob operators opted out of.
- `resolution_to_dto` computes `blocking` fresh against the repo policy and
  the human override. A stored `blocking` bit would drift when the policy
  changes; computing on read is the whole point of §4 "blocking is computed,
  not stored".
"""

from datetime import UTC, datetime

from domains.delivery.models import DEFAULT_PATH_DENYLIST, FindingResolution, MergePolicy
from domains.delivery.schemas import BlockingOverride, ResolutionState
from domains.delivery.services.resolution_service import (
    merge_policy_to_dto,
    resolution_to_dto,
)
from domains.findings.models import Finding

POLICY = {"default": "high", "per_tag": {"security": "medium"}}


def _policy(**over) -> MergePolicy:
    base = dict(uid="mp1", repository_uid="repo1")
    base.update(over)
    return MergePolicy(**base)


def _resolution(**over) -> FindingResolution:
    base = dict(
        uid="res1",
        finding_uid="f1",
        pull_request_uid="pr1",
        repository_uid="repo1",
        resolution_key="f1:pr1",
    )
    base.update(over)
    return FindingResolution(**base)


def _finding(**over) -> Finding:
    base = dict(
        uid="f1",
        repository_uid="repo1",
        title="finding",
        kind="bug",
        severity="high",
        tags=["correctness"],
    )
    base.update(over)
    return Finding(**base)


# ── MergePolicy → DTO ────────────────────────────────────────────────────────


def test_merge_policy_to_dto_carries_configured_fields():
    dto = merge_policy_to_dto(_policy(blocking=POLICY, max_fix_rounds=3))
    assert dto.uid == "mp1"
    assert dto.repository_uid == "repo1"
    assert dto.blocking == POLICY
    assert dto.require_clean_round is True
    assert dto.max_fix_rounds == 3


def test_merge_policy_to_dto_none_denylist_falls_back_to_defaults():
    """A row saved before path_denylist existed has None on the property; the
    DTO surfaces the shipped defaults so the operator sees what actually
    guards the write path."""
    dto = merge_policy_to_dto(_policy(path_denylist=None))
    assert dto.path_denylist == list(DEFAULT_PATH_DENYLIST)


def test_merge_policy_to_dto_empty_denylist_is_an_opt_out_and_is_honoured():
    """[] is DIFFERENT from None — operator explicitly cleared the list. The
    DTO must not "helpfully" refill it with the defaults, or the opt-out is
    a lie."""
    dto = merge_policy_to_dto(_policy(path_denylist=[]))
    assert dto.path_denylist == []


def test_merge_policy_to_dto_denylist_is_string_coerced():
    """A JSON round-trip can leave non-strings in the list; the DTO enforces
    the contract downstream code depends on (re.compile takes strings)."""
    dto = merge_policy_to_dto(_policy(path_denylist=["(^|/)foo/", 42]))
    assert dto.path_denylist == ["(^|/)foo/", "42"]


# ── FindingResolution → DTO ──────────────────────────────────────────────────


def test_resolution_to_dto_computes_blocking_from_policy_and_finding():
    """The stored resolution has no `blocking` bit — the DTO computes it from
    (state, severity, tags, override) against the current policy so a policy
    edit takes effect on the very next read."""
    dto = resolution_to_dto(_resolution(state="open"), _finding(severity="high"), POLICY)
    assert dto.state == ResolutionState.OPEN
    assert dto.blocking is True
    assert dto.finding_title == "finding"
    assert dto.finding_severity == "high"
    assert dto.finding_tags == ["correctness"]


def test_resolution_to_dto_terminal_states_never_block():
    for state in ("verified", "deferred", "waived", "refuted"):
        dto = resolution_to_dto(
            _resolution(state=state), _finding(severity="critical"), POLICY
        )
        assert dto.blocking is False, f"{state} must not read as blocking"


def test_resolution_to_dto_human_override_flips_blocking_both_ways():
    """`allow` clears a critical block; `block` forces a docs-tagged low."""
    allowed = resolution_to_dto(
        _resolution(state="open", blocking_override="allow"),
        _finding(severity="critical", tags=["security"]),
        POLICY,
    )
    forced = resolution_to_dto(
        _resolution(state="open", blocking_override="block"),
        _finding(severity="low", tags=["docs"]),
        POLICY,
    )
    assert allowed.blocking is False
    assert forced.blocking is True
    assert allowed.blocking_override == BlockingOverride.ALLOW
    assert forced.blocking_override == BlockingOverride.BLOCK


def test_resolution_to_dto_handles_a_missing_finding():
    """The finding may have been deleted after the resolution was bound;
    the DTO must default (severity=medium, no tags) rather than crash — the
    resolution row itself is the source of truth for lifecycle state."""
    dto = resolution_to_dto(_resolution(state="open"), None, POLICY)
    assert dto.finding_title == ""
    assert dto.finding_severity == "medium"
    assert dto.finding_tags == []
    # medium at the default `high` threshold is not blocking.
    assert dto.blocking is False


def test_resolution_to_dto_carries_lifecycle_facets_verbatim():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    r = _resolution(
        state="fixed",
        introduced_at_sha="a" * 40,
        fixed_at_sha="b" * 40,
        verified_at_sha="c" * 40,
        verified_by_run_uid="run-1",
        waived_by="alice",
        waive_reason="known false positive",
        waive_requested_by="bob",
        waive_requested_reason="looks resolved",
        blocking_override_reason="override justification",
        ticket_uid="t-99",
        created_at=now,
        updated_at=now,
    )
    dto = resolution_to_dto(r, _finding(severity="high"), POLICY)
    assert dto.introduced_at_sha == "a" * 40
    assert dto.fixed_at_sha == "b" * 40
    assert dto.verified_at_sha == "c" * 40
    assert dto.verified_by_run_uid == "run-1"
    assert dto.waived_by == "alice"
    assert dto.waive_reason == "known false positive"
    assert dto.waive_requested_by == "bob"
    assert dto.waive_requested_reason == "looks resolved"
    assert dto.blocking_override_reason == "override justification"
    assert dto.ticket_uid == "t-99"
    assert dto.created_at == now
    assert dto.updated_at == now
