"""Pure-function tests for finding list sorting (sort_findings)."""

from datetime import UTC, datetime

from domains.findings.schemas import FindingDTO, FindingKind, Severity
from domains.findings.services.finding_service import (
    FINDING_SORT_DIRS,
    FINDING_SORT_FIELDS,
    severity_rank,
    sort_findings,
)


def _dto(
    uid: str,
    *,
    severity: str = "medium",
    confidence: float = 0.7,
    title: str = "t",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> FindingDTO:
    return FindingDTO(
        uid=uid,
        repository_uid="repo1",
        kind=FindingKind.DEFECT,
        severity=Severity(severity),
        confidence=confidence,
        title=title,
        dedupe_key=f"key-{uid}",
        created_at=created_at,
        updated_at=updated_at,
    )


def _ts(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


def test_default_sort_is_updated_at_desc():
    items = [
        _dto("a", updated_at=_ts(1)),
        _dto("b", updated_at=_ts(3)),
        _dto("c", updated_at=_ts(2)),
    ]
    assert [f.uid for f in sort_findings(items)] == ["b", "c", "a"]


def test_updated_at_falls_back_to_created_at_and_none_sorts_last_on_desc():
    items = [
        _dto("no-dates"),
        _dto("created-only", created_at=_ts(5)),
        _dto("updated", updated_at=_ts(2)),
    ]
    assert [f.uid for f in sort_findings(items)] == ["created-only", "updated", "no-dates"]


def test_severity_desc_orders_critical_first_recency_breaks_ties():
    items = [
        _dto("low", severity="low", updated_at=_ts(9)),
        _dto("crit", severity="critical", updated_at=_ts(1)),
        _dto("high-old", severity="high", updated_at=_ts(2)),
        _dto("high-new", severity="high", updated_at=_ts(4)),
    ]
    out = sort_findings(items, sort_by="severity", sort_dir="desc")
    assert [f.uid for f in out] == ["crit", "high-new", "high-old", "low"]


def test_severity_asc_orders_low_first():
    items = [_dto("crit", severity="critical"), _dto("low", severity="low")]
    out = sort_findings(items, sort_by="severity", sort_dir="asc")
    assert [f.uid for f in out] == ["low", "crit"]


def test_confidence_sort():
    items = [_dto("a", confidence=0.2), _dto("b", confidence=0.9), _dto("c", confidence=0.5)]
    assert [f.uid for f in sort_findings(items, sort_by="confidence")] == ["b", "c", "a"]
    assert [
        f.uid for f in sort_findings(items, sort_by="confidence", sort_dir="asc")
    ] == ["a", "c", "b"]


def test_title_sort_is_case_insensitive_and_asc_by_default_direction_semantics():
    items = [_dto("b", title="beta"), _dto("a", title="Alpha"), _dto("z", title="zeta")]
    out = sort_findings(items, sort_by="title", sort_dir="asc")
    assert [f.uid for f in out] == ["a", "b", "z"]
    out = sort_findings(items, sort_by="title", sort_dir="desc")
    assert [f.uid for f in out] == ["z", "b", "a"]


def test_created_at_sort():
    items = [_dto("a", created_at=_ts(2)), _dto("b", created_at=_ts(4)), _dto("c")]
    assert [f.uid for f in sort_findings(items, sort_by="created_at")] == ["b", "a", "c"]


def test_sort_does_not_mutate_input():
    items = [_dto("a", updated_at=_ts(1)), _dto("b", updated_at=_ts(2))]
    sort_findings(items)
    assert [f.uid for f in items] == ["a", "b"]


def test_severity_rank_unknown_defaults_to_medium():
    assert severity_rank("nonsense") == severity_rank("medium")
    assert severity_rank("critical") > severity_rank("high") > severity_rank("medium") > severity_rank("low")


def test_whitelists_cover_expected_values():
    assert FINDING_SORT_FIELDS == {
        "updated_at",
        "created_at",
        "severity",
        "confidence",
        "title",
        # Composite credibility — corroboration + tool confirmation +
        # confidence + skeptic outcome (findings/services/trust.py).
        "trust",
    }
    assert FINDING_SORT_DIRS == {"asc", "desc"}
