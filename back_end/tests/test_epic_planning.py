"""Pure-function tests for the ticket epic planning core (selection + axes)."""

from datetime import UTC, datetime

import pytest

from domains.tickets.services.epics.axes import partition
from domains.tickets.services.epics.loader import normalize_path
from domains.tickets.services.epics.schemas import (
    AGENT_AXES,
    DETERMINISTIC_AXES,
    MAX_RATIONALE_CHARS,
    EpicAxis,
    EpicPartition,
    EpicSelection,
    TicketFacts,
)
from domains.tickets.services.epics.selection import select_tickets


def _t(uid: str, **kwargs) -> TicketFacts:
    return TicketFacts(uid=uid, title=f"ticket {uid}", **kwargs)


def _ts(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=UTC)


def _uids(facts: list[TicketFacts]) -> list[str]:
    return [f.uid for f in facts]


# ── selection: filters ─────────────────────────────────────────────────────


def test_status_filter_keeps_only_listed_statuses():
    pool = [_t("a", status="backlog"), _t("b", status="done"), _t("c", status="todo")]
    out = select_tickets(pool, EpicSelection(statuses=("backlog", "todo")))
    assert sorted(_uids(out)) == ["a", "c"]


def test_empty_statuses_is_no_constraint():
    pool = [_t("a", status="done"), _t("b", status="backlog")]
    out = select_tickets(pool, EpicSelection(statuses=()))
    assert sorted(_uids(out)) == ["a", "b"]


def test_min_priority_is_a_ladder_not_an_exact_match():
    pool = [
        _t("low", priority="low"),
        _t("medium", priority="medium"),
        _t("high", priority="high"),
        _t("urgent", priority="urgent"),
    ]
    out = select_tickets(pool, EpicSelection(min_priority="high"))
    assert sorted(_uids(out)) == ["high", "urgent"]


def test_min_priority_boundary_is_inclusive():
    pool = [_t("high", priority="high"), _t("medium", priority="medium")]
    out = select_tickets(pool, EpicSelection(min_priority="high"))
    assert _uids(out) == ["high"]


def test_min_severity_is_a_ladder_and_boundary_is_inclusive():
    pool = [
        _t("low", severity="low"),
        _t("medium", severity="medium"),
        _t("high", severity="high"),
        _t("critical", severity="critical"),
    ]
    out = select_tickets(pool, EpicSelection(min_severity="high"))
    assert sorted(_uids(out)) == ["critical", "high"]


def test_unset_min_filters_admit_everything_including_blank_values():
    pool = [_t("blank"), _t("low", priority="low", severity="low")]
    out = select_tickets(pool, EpicSelection(min_priority="", min_severity=""))
    assert sorted(_uids(out)) == ["blank", "low"]


def test_unrecognized_minimum_admits_everything():
    pool = [_t("a", priority="low"), _t("b", priority="urgent")]
    out = select_tickets(pool, EpicSelection(min_priority="nonsense"))
    assert sorted(_uids(out)) == ["a", "b"]


def test_labels_filter_keeps_tickets_holding_any_listed_label():
    pool = [
        _t("both", labels=("security", "perf")),
        _t("one", labels=("perf",)),
        _t("none", labels=("docs",)),
        _t("bare"),
    ]
    out = select_tickets(pool, EpicSelection(labels=("security", "perf")))
    assert sorted(_uids(out)) == ["both", "one"]


def test_empty_labels_is_no_constraint():
    pool = [_t("a"), _t("b", labels=("docs",))]
    assert sorted(_uids(select_tickets(pool, EpicSelection()))) == ["a", "b"]


def test_kinds_filter():
    pool = [_t("d", kind="defect"), _t("g", kind="gap"), _t("blank")]
    out = select_tickets(pool, EpicSelection(kinds=("defect",)))
    assert _uids(out) == ["d"]
    assert sorted(_uids(select_tickets(pool, EpicSelection(kinds=())))) == [
        "blank",
        "d",
        "g",
    ]


def test_area_keys_match_exactly_or_nest_but_never_by_bare_prefix():
    pool = [
        _t("exact", area_keys=("backend",)),
        _t("child", area_keys=("backend/api",)),
        _t("deep", area_keys=("backend/api/v1",)),
        _t("sibling", area_keys=("backend-jobs",)),
        _t("other", area_keys=("frontend",)),
        _t("keyless"),
    ]
    out = select_tickets(pool, EpicSelection(area_keys=("backend",)))
    assert sorted(_uids(out)) == ["child", "deep", "exact"]


def test_area_keys_match_when_any_of_the_tickets_areas_matches():
    pool = [_t("multi", area_keys=("frontend", "backend/api"))]
    assert _uids(select_tickets(pool, EpicSelection(area_keys=("backend",)))) == ["multi"]


def test_filters_compose_as_and():
    pool = [
        _t("keep", priority="high", labels=("security",), area_keys=("backend/api",)),
        _t("wrong-label", priority="high", labels=("docs",), area_keys=("backend/api",)),
        _t("wrong-prio", priority="low", labels=("security",), area_keys=("backend/api",)),
        _t("wrong-area", priority="high", labels=("security",), area_keys=("frontend",)),
    ]
    spec = EpicSelection(
        min_priority="high", labels=("security",), area_keys=("backend",)
    )
    assert _uids(select_tickets(pool, spec)) == ["keep"]


# ── selection: ordering ────────────────────────────────────────────────────


def test_priority_sort_is_rank_desc_then_recency_desc():
    pool = [
        _t("low", priority="low", updated_at=_ts(9)),
        _t("urgent", priority="urgent", updated_at=_ts(1)),
        _t("high-old", priority="high", updated_at=_ts(2)),
        _t("high-new", priority="high", updated_at=_ts(4)),
    ]
    out = select_tickets(pool, EpicSelection(sort="priority"))
    assert _uids(out) == ["urgent", "high-new", "high-old", "low"]


def test_severity_sort_is_rank_desc_then_recency_desc():
    pool = [
        _t("low", severity="low", updated_at=_ts(9)),
        _t("crit", severity="critical", updated_at=_ts(1)),
        _t("high-old", severity="high", updated_at=_ts(2)),
        _t("high-new", severity="high", updated_at=_ts(4)),
    ]
    out = select_tickets(pool, EpicSelection(sort="severity"))
    assert _uids(out) == ["crit", "high-new", "high-old", "low"]


def test_age_sort_is_oldest_first_and_undated_last():
    pool = [
        _t("new", updated_at=_ts(9)),
        _t("undated"),
        _t("old", updated_at=_ts(1)),
    ]
    assert _uids(select_tickets(pool, EpicSelection(sort="age"))) == [
        "old",
        "new",
        "undated",
    ]


def test_undated_tickets_rank_last_on_priority_ties_too():
    pool = [
        _t("undated", priority="high"),
        _t("dated", priority="high", updated_at=_ts(1)),
    ]
    assert _uids(select_tickets(pool, EpicSelection())) == ["dated", "undated"]


def test_size_sort_is_smallest_first_and_unsized_ranks_as_medium():
    pool = [
        _t("large", size="large"),
        _t("trivial", size="trivial"),
        _t("unsized"),
        _t("small", size="small"),
    ]
    out = select_tickets(pool, EpicSelection(sort="size"))
    assert _uids(out) == ["trivial", "small", "unsized", "large"]


def test_ties_break_on_uid_so_the_order_is_total():
    pool = [_t("c"), _t("a"), _t("b")]
    assert _uids(select_tickets(pool, EpicSelection())) == ["a", "b", "c"]


def test_identical_input_produces_identical_output_on_every_sort():
    pool = [
        _t("c", priority="high", severity="high", size="small", updated_at=_ts(2)),
        _t("a", priority="high", severity="high", size="small", updated_at=_ts(2)),
        _t("b", priority="high", severity="high", size="small", updated_at=_ts(2)),
    ]
    for sort in ("priority", "severity", "age", "size"):
        first = _uids(select_tickets(pool, EpicSelection(sort=sort)))
        second = _uids(select_tickets(list(reversed(pool)), EpicSelection(sort=sort)))
        assert first == second == ["a", "b", "c"]


def test_unknown_sort_key_falls_back_to_priority():
    pool = [_t("low", priority="low"), _t("urgent", priority="urgent")]
    assert _uids(select_tickets(pool, EpicSelection(sort="bogus"))) == ["urgent", "low"]


def test_limit_is_applied_after_sorting_and_zero_is_uncapped():
    pool = [
        _t("low", priority="low"),
        _t("urgent", priority="urgent"),
        _t("high", priority="high"),
    ]
    assert _uids(select_tickets(pool, EpicSelection(limit=2))) == ["urgent", "high"]
    assert len(select_tickets(pool, EpicSelection(limit=0))) == 3


def test_select_does_not_mutate_its_input():
    pool = [_t("c"), _t("a")]
    select_tickets(pool, EpicSelection())
    assert _uids(pool) == ["c", "a"]


# ── axes: grouping ─────────────────────────────────────────────────────────


def _spec(axis: EpicAxis, **kwargs) -> EpicPartition:
    return EpicPartition(axis=axis, **kwargs)


def _members(drafts) -> list[list[str]]:
    return [d.member_ticket_uids for d in drafts]


def test_by_area_groups_on_the_most_specific_area_key():
    pool = [
        _t("a", area_keys=("backend", "backend/api")),
        _t("b", area_keys=("backend/api",)),
        _t("c", area_keys=("backend",)),
        _t("d", area_keys=("backend",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.AREA))
    by_key = {d.evidence["area_key"]: d.member_ticket_uids for d in drafts}
    assert by_key == {"backend/api": ["a", "b"], "backend": ["c", "d"]}


def test_a_ticket_never_lands_in_two_epics():
    pool = [
        _t("a", area_keys=("backend", "backend/api", "backend/api/v1")),
        _t("b", area_keys=("backend/api/v1",)),
        _t("c", area_keys=("backend",)),
        _t("d", area_keys=("backend",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.AREA))
    placed = [uid for d in drafts for uid in d.member_ticket_uids]
    assert sorted(placed) == sorted(set(placed))


def test_tickets_without_an_area_key_form_no_epic():
    pool = [_t("a"), _t("b")]
    assert partition(pool, _spec(EpicAxis.AREA)) == []


def test_by_feature_groups_on_the_feature_axis_independently_of_areas():
    pool = [
        _t("a", area_keys=("backend",), feature_keys=("signup",)),
        _t("b", area_keys=("frontend",), feature_keys=("signup", "signup/oauth")),
        _t("c", feature_keys=("billing",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.FEATURE, min_members=1))
    by_key = {d.evidence["feature_key"]: d.member_ticket_uids for d in drafts}
    assert by_key == {"signup": ["a"], "signup/oauth": ["b"], "billing": ["c"]}


def test_by_lens_groups_on_kind():
    pool = [
        _t("a", kind="defect"),
        _t("b", kind="defect"),
        _t("c", kind="gap"),
        _t("d", kind="gap"),
        _t("e"),
    ]
    drafts = partition(pool, _spec(EpicAxis.LENS))
    by_key = {d.evidence["lens"]: d.member_ticket_uids for d in drafts}
    assert by_key == {"defect": ["a", "b"], "gap": ["c", "d"]}


def test_by_class_groups_on_the_tag_slash_subtype_key():
    pool = [
        _t("a", tags=("security",), subtype="missing-authz"),
        _t("b", tags=("security",), subtype="missing-authz"),
    ]
    drafts = partition(pool, _spec(EpicAxis.CLASS))
    assert [d.evidence["class"] for d in drafts] == ["security/missing-authz"]


def test_by_class_assigns_a_multi_tagged_ticket_to_its_strongest_class_only():
    pool = [
        _t("a", tags=("security", "perf"), subtype="n-plus-one"),
        _t("b", tags=("perf",), subtype="n-plus-one"),
        _t("c", tags=("perf",), subtype="n-plus-one"),
    ]
    drafts = partition(pool, _spec(EpicAxis.CLASS))
    assert len(drafts) == 1
    assert drafts[0].evidence["class"] == "perf/n-plus-one"
    assert drafts[0].member_ticket_uids == ["a", "b", "c"]


def test_by_class_ignores_tickets_without_a_subtype_or_tags():
    pool = [
        _t("no-subtype", tags=("security",)),
        _t("no-tags", subtype="missing-authz"),
        _t("classed", tags=("security",), subtype="missing-authz"),
    ]
    drafts = partition(pool, _spec(EpicAxis.CLASS, min_members=1))
    assert _members(drafts) == [["classed"]]


def test_by_files_is_transitive_across_a_chain_of_shared_paths():
    pool = [
        _t("a", paths=("svc.py", "one.py")),
        _t("b", paths=("svc.py", "client.py")),
        _t("c", paths=("client.py", "three.py")),
    ]
    drafts = partition(pool, _spec(EpicAxis.FILES))
    assert _members(drafts) == [["a", "b", "c"]]
    assert drafts[0].evidence["shared_paths"] == ["client.py", "svc.py"]
    assert drafts[0].evidence["shared_path_count"] == 2


def test_by_files_keeps_disjoint_path_sets_apart():
    pool = [
        _t("a", paths=("x.py",)),
        _t("b", paths=("x.py",)),
        _t("c", paths=("y.py",)),
        _t("d", paths=("y.py",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.FILES))
    assert sorted(_members(drafts)) == [["a", "b"], ["c", "d"]]


def test_by_files_drops_tickets_that_share_nothing():
    pool = [
        _t("a", paths=("x.py",)),
        _t("b", paths=("x.py",)),
        _t("lonely", paths=("z.py",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.FILES))
    assert _members(drafts) == [["a", "b"]]


def test_by_linked_is_transitive_across_shared_finding_uids():
    pool = [
        _t("a", finding_uids=("f1",)),
        _t("b", finding_uids=("f1", "f2")),
        _t("c", finding_uids=("f2",)),
        _t("d", finding_uids=("f9",)),
    ]
    drafts = partition(pool, _spec(EpicAxis.LINKED))
    assert _members(drafts) == [["a", "b", "c"]]
    assert drafts[0].evidence["shared_finding_uids"] == ["f1", "f2"]


# ── axes: sizing, ranking, capping ─────────────────────────────────────────


def test_groups_below_min_members_are_dropped():
    pool = [
        _t("a", kind="defect"),
        _t("b", kind="defect"),
        _t("solo", kind="gap"),
    ]
    drafts = partition(pool, _spec(EpicAxis.LENS, min_members=2))
    assert _members(drafts) == [["a", "b"]]


def test_min_members_of_one_keeps_singletons():
    pool = [_t("solo", kind="gap")]
    drafts = partition(pool, _spec(EpicAxis.LENS, min_members=1))
    assert _members(drafts) == [["solo"]]


def test_oversized_groups_split_into_near_equal_chunks_without_an_orphan():
    pool = [_t(f"t{i}", kind="defect") for i in range(7)]
    drafts = partition(pool, _spec(EpicAxis.LENS, min_members=2, max_members=6))
    assert sorted(len(d.member_ticket_uids) for d in drafts) == [3, 4]
    placed = [uid for d in drafts for uid in d.member_ticket_uids]
    assert sorted(placed) == sorted(_uids(pool))


def test_split_chunks_are_labelled_as_parts_of_the_whole():
    pool = [_t(f"t{i}", kind="defect") for i in range(7)]
    drafts = partition(pool, _spec(EpicAxis.LENS, min_members=2, max_members=6))
    assert {d.evidence["of_parts"] for d in drafts} == {2}
    assert sorted(d.evidence["part"] for d in drafts) == [1, 2]
    assert drafts[0].title.endswith("(1/2)")


def test_a_split_remainder_below_min_members_is_dropped():
    pool = [_t(f"t{i}", kind="defect") for i in range(3)]
    drafts = partition(pool, _spec(EpicAxis.LENS, min_members=2, max_members=2))
    assert sorted(len(d.member_ticket_uids) for d in drafts) == [2]


def test_max_epics_keeps_the_strongest_groups_first():
    pool = (
        [_t(f"big{i}", kind="a") for i in range(4)]
        + [_t(f"mid{i}", kind="b") for i in range(3)]
        + [_t(f"small{i}", kind="c") for i in range(2)]
    )
    drafts = partition(pool, _spec(EpicAxis.LENS, max_epics=2))
    assert [len(d.member_ticket_uids) for d in drafts] == [4, 3]


def test_equal_sized_groups_rank_by_aggregate_priority_then_label():
    pool = [
        _t("u1", kind="b", priority="urgent"),
        _t("u2", kind="b", priority="urgent"),
        _t("l1", kind="a", priority="low"),
        _t("l2", kind="a", priority="low"),
        _t("m1", kind="c", priority="medium"),
        _t("m2", kind="c", priority="medium"),
    ]
    drafts = partition(pool, _spec(EpicAxis.LENS))
    assert [d.evidence["lens"] for d in drafts] == ["b", "c", "a"]


def test_capping_is_visible_to_the_caller():
    pool = (
        [_t(f"a{i}", kind="a") for i in range(3)]
        + [_t(f"b{i}", kind="b") for i in range(2)]
        + [_t(f"c{i}", kind="c") for i in range(2)]
    )
    drafts = partition(pool, _spec(EpicAxis.LENS, max_epics=1))
    assert len(drafts) == 1
    assert drafts[0].evidence["capped_from"] == 3


def test_no_cap_marker_when_nothing_was_dropped():
    pool = [_t("a", kind="a"), _t("b", kind="a")]
    drafts = partition(pool, _spec(EpicAxis.LENS, max_epics=4))
    assert "capped_from" not in drafts[0].evidence


def test_zero_max_epics_is_uncapped():
    pool = [_t(f"{k}{i}", kind=k) for k in "abcdef" for i in range(2)]
    drafts = partition(pool, _spec(EpicAxis.LENS, max_epics=0))
    assert len(drafts) == 6


def test_partition_is_deterministic_across_repeated_calls():
    pool = [
        _t("a", kind="x", paths=("p.py",)),
        _t("b", kind="x", paths=("p.py",)),
        _t("c", kind="y", paths=("q.py",)),
        _t("d", kind="y", paths=("q.py",)),
    ]
    spec = _spec(EpicAxis.FILES, max_epics=2)
    assert _members(partition(pool, spec)) == _members(partition(pool, spec))


# ── axes: non-computable axes ──────────────────────────────────────────────


# Driven off `AGENT_AXES` rather than a hand-written list: a new agent axis
# must be refused here the day it is added, not the day someone remembers to
# extend this test.
@pytest.mark.parametrize("axis", [EpicAxis.MANUAL, *sorted(AGENT_AXES)])
def test_non_deterministic_axes_raise_rather_than_return_nothing(axis):
    pool = [_t("a", kind="defect"), _t("b", kind="defect")]
    with pytest.raises(ValueError, match=axis.value):
        partition(pool, _spec(axis))


def test_every_deterministic_axis_is_computable():
    pool = [_t("a", kind="k", paths=("p",), finding_uids=("f",))]
    for axis in DETERMINISTIC_AXES:
        partition(pool, _spec(axis, min_members=1))  # must not raise


# ── drafts: evidence, rationale ────────────────────────────────────────────




def test_evidence_leads_with_the_identifying_fact_and_stays_small():
    pool = [_t("a", area_keys=("backend/areas",)), _t("b", area_keys=("backend/areas",))]
    evidence = partition(pool, _spec(EpicAxis.AREA))[0].evidence
    assert next(iter(evidence)) == "area_key"
    assert evidence == {
        "area_key": "backend/areas",
        "axis": "area",
        "member_count": 2,
    }


def test_shared_path_evidence_is_sampled_not_dumped():
    paths = tuple(f"p{i}.py" for i in range(10))
    pool = [_t("a", paths=paths), _t("b", paths=paths)]
    evidence = partition(pool, _spec(EpicAxis.FILES))[0].evidence
    assert len(evidence["shared_paths"]) == 3
    assert evidence["shared_path_count"] == 10


def test_title_reads_as_key_then_member_count():
    pool = [_t("a", area_keys=("backend/areas",)), _t("b", area_keys=("backend/areas",))]
    assert partition(pool, _spec(EpicAxis.AREA))[0].title == "backend/areas · 2 tickets"


def test_rationale_is_one_short_sentence_within_the_cap():
    pool = [_t("a", paths=("x" * 400,)), _t("b", paths=("x" * 400,))]
    draft = partition(pool, _spec(EpicAxis.FILES))[0]
    assert 0 < len(draft.rationale) <= MAX_RATIONALE_CHARS


def test_draft_inherits_its_most_urgent_member_priority():
    pool = [
        _t("a", kind="k", priority="low"),
        _t("b", kind="k", priority="urgent"),
    ]
    assert partition(pool, _spec(EpicAxis.LENS))[0].suggested_priority == "urgent"


def test_suggested_labels_are_the_labels_every_member_shares():
    pool = [
        _t("a", kind="k", labels=("security", "backend")),
        _t("b", kind="k", labels=("security", "frontend")),
    ]
    assert partition(pool, _spec(EpicAxis.LENS))[0].suggested_labels == ["security"]


def test_selection_and_partition_compose():
    pool = [
        _t("a", status="backlog", priority="high", area_keys=("backend/api",)),
        _t("b", status="backlog", priority="high", area_keys=("backend/api",)),
        _t("c", status="done", priority="high", area_keys=("backend/api",)),
        _t("d", status="backlog", priority="low", area_keys=("backend/api",)),
    ]
    selected = select_tickets(pool, EpicSelection(min_priority="high"))
    drafts = partition(selected, EpicPartition(axis=EpicAxis.AREA))
    assert _members(drafts) == [["a", "b"]]


# ── Path normalization ──────────────────────────────────────────────────────
#
# WHY: `Finding.affected_paths` entries carry line anchors —
# "back_end/api/v1/platform_tools.py:549-561". In the reference repository
# 30 of 30 findings have one. The `files` axis intersects paths as plain
# strings, so without normalization two tickets editing the SAME file at
# different lines share nothing and are never grouped. The axis fails
# silently, reading as "nothing overlaps" rather than as a bug.


def test_normalize_path_strips_a_line_range():
    assert normalize_path("a/b/c.py:549-561") == "a/b/c.py"


def test_normalize_path_strips_a_single_line():
    assert normalize_path("a/b/c.py:26") == "a/b/c.py"


def test_normalize_path_leaves_a_bare_path_alone():
    assert normalize_path("a/b/c.py") == "a/b/c.py"


def test_normalize_path_keeps_non_anchor_colons():
    """Only a TRAILING numeric anchor is an anchor. A colon elsewhere belongs
    to the name and stripping it would corrupt the path."""
    assert normalize_path("weird:name/c.py") == "weird:name/c.py"
    assert normalize_path("a/b:2/c.py") == "a/b:2/c.py"


def test_normalize_path_handles_empty_and_whitespace():
    assert normalize_path("") == ""
    assert normalize_path("  a/b.py:10  ") == "a/b.py"


def test_same_file_different_lines_lands_in_one_epic():
    """The end-to-end point of the normalization: these two would not epic
    if the anchors survived into the axis."""
    facts = [
        _t("t1", paths=(normalize_path("back_end/api/v1/platform_tools.py:549-561"),)),
        _t("t2", paths=(normalize_path("back_end/api/v1/platform_tools.py:20-30"),)),
    ]
    drafts = partition(facts, EpicPartition(axis=EpicAxis.FILES, max_epics=4))
    assert len(drafts) == 1
    assert sorted(drafts[0].member_ticket_uids) == ["t1", "t2"]
