"""Campaign planner — pure partition + template math."""

from datetime import UTC, datetime, timedelta

from domains.campaigns.services.planner import build_plan_by_kind, normalize_areas

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _doc(uid, watch, *, title="", slug=None):
    return {
        "uid": uid,
        "slug": uid if slug is None else slug,
        "title": title or uid,
        "watch_paths": watch,
    }


def _matched(area, paths):
    """The files an area's scope actually covers (planner scope semantics)."""
    from domains.repositories.services.path_matching import watches_path

    return {p for p in paths if watches_path(area["scope_paths"], p)}


# ── normalize_areas ──────────────────────────────────────────────────────────


def test_right_sized_area_stays_whole():
    paths = [f"src/api/f{i}.py" for i in range(60)]
    areas = normalize_areas([_doc("d1", ["src/api"])], paths)
    assert len(areas) == 1
    assert areas[0]["scope_paths"] == ["src/api"]
    assert areas[0]["doc_uids"] == ["d1"]
    assert areas[0]["file_count"] == 60


def test_oversized_area_splits_by_first_level_subdir():
    paths = [f"src/a/f{i}.py" for i in range(100)] + [f"src/b/f{i}.py" for i in range(100)]
    areas = normalize_areas([_doc("d1", ["src"], title="Source")], paths)
    assert len(areas) == 2
    assert {tuple(a["scope_paths"]) for a in areas} == {("src/a",), ("src/b",)}
    for a in areas:
        assert a["doc_uids"] == ["d1"]
        assert a["title"].startswith("Source — ")
        assert a["file_count"] == 100


def test_adjacent_tiny_same_branch_areas_merge():
    paths = [f"a/f{i}.py" for i in range(10)] + [f"b/f{i}.py" for i in range(10)]
    areas = normalize_areas(
        [_doc("d1", ["a"], slug="app/a"), _doc("d2", ["b"], slug="app/b")],
        paths,
        target_min=15,
        target_max=150,
    )
    assert len(areas) == 1
    assert areas[0]["scope_paths"] == ["a", "b"]
    assert areas[0]["doc_uids"] == ["d1", "d2"]
    assert areas[0]["file_count"] == 20
    assert " + " in areas[0]["title"]


def test_tiny_areas_from_different_branches_never_merge():
    # backend/* pages may merge with each other; frontend/* and top-level
    # slugless pages stay separate — never "LLM providers + Repositories".
    paths = ["be/a/f.py", "be/b/f.py", "fe/c/f.py", "conv.md"]
    areas = normalize_areas(
        [
            _doc("d1", ["be/a"], slug="backend/a"),
            _doc("d2", ["be/b"], slug="backend/b"),
            _doc("d3", ["fe/c"], slug="frontend/c"),
            _doc("d4", ["conv.md"], slug="conventions"),
        ],
        paths,
        target_min=10,
    )
    by_uids = {tuple(a["doc_uids"]): a for a in areas}
    assert ("d1", "d2") in by_uids  # backend branch-mates merged
    assert ("d3",) in by_uids  # frontend tiny stays its own part
    assert ("d4",) in by_uids  # slugless top-level page is its own segment
    assert len(areas) == 3


def test_unwatched_paths_land_in_remainder():
    paths = ["src/f.py", "scripts/x.sh", "scripts/y.sh", "README.md"]
    areas = normalize_areas([_doc("d1", ["src"])], paths, target_min=1)
    remainder = [a for a in areas if a["title"] == "Uncovered paths"]
    assert len(remainder) == 1
    assert remainder[0]["doc_uids"] == []
    assert remainder[0]["file_count"] == 3
    assert set(remainder[0]["scope_paths"]) == {"scripts", "README.md"}


def test_oversized_remainder_splits_too():
    paths = [f"x/f{i}.py" for i in range(100)] + [f"y/f{i}.py" for i in range(100)]
    areas = normalize_areas([_doc("d1", ["src"])], paths, target_min=10, target_max=150)
    remainders = [a for a in areas if a["title"].startswith("Uncovered paths")]
    assert len(remainders) == 2
    assert {tuple(a["scope_paths"]) for a in remainders} == {("x",), ("y",)}


def test_empty_tree_degrades_to_one_part_per_doc():
    areas = normalize_areas([_doc("d1", ["src"]), _doc("d2", ["lib"])], [])
    assert len(areas) == 2
    assert all(a["file_count"] is None for a in areas)
    # No remainder can exist without a tree.
    assert all(a["title"] != "Uncovered paths" for a in areas)


def test_docs_without_watch_paths_are_skipped():
    assert normalize_areas([_doc("d1", [])], []) == []


# ── exclusive ownership ──────────────────────────────────────────────────────


def test_identical_prefix_dedup_smallest_claim_wins():
    # Two docs watch back_end/domains-style identical prefixes: the page with
    # the smallest total claim (most specific) wins; the overview only keeps
    # what it exclusively owns — no directory is audited twice.
    paths = [f"src/f{i}.py" for i in range(10)] + [f"lib/f{i}.py" for i in range(5)]
    areas = normalize_areas(
        [
            _doc("overview", ["src", "lib"], slug="overview"),
            _doc("srcpage", ["src"], slug="srcpage"),
        ],
        paths,
        target_min=1,
    )
    assert len(areas) == 2
    by_uids = {tuple(a["doc_uids"]): a for a in areas}
    assert by_uids[("srcpage",)]["scope_paths"] == ["src"]
    assert by_uids[("srcpage",)]["file_count"] == 10
    assert by_uids[("overview",)]["scope_paths"] == ["lib"]
    assert by_uids[("overview",)]["file_count"] == 5
    matched = [_matched(a, paths) for a in areas]
    assert matched[0].isdisjoint(matched[1])


def test_identical_prefix_tie_breaks_to_first_slug():
    paths = ["src/a.py", "src/b.py"]
    areas = normalize_areas(
        [
            _doc("dz", ["src"], slug="zeta"),
            _doc("da", ["src"], slug="alpha"),
        ],
        paths,
        target_min=1,
    )
    assert len(areas) == 1
    assert areas[0]["doc_uids"] == ["da"]
    assert areas[0]["file_count"] == 2


def test_most_specific_prefix_owns_its_files_and_scopes_stay_disjoint():
    # domains vs domains/runs: the deeper page owns the runs files; the
    # shallow page's scope is refined to exclude them (subdir prefix +
    # individual file paths where refinement bottoms out).
    paths = [
        "domains/core/a.py",
        "domains/core/b.py",
        "domains/top.py",
        "domains/runs/r1.py",
        "domains/runs/r2.py",
        "domains/runs/deep/r3.py",
    ]
    areas = normalize_areas(
        [
            _doc("shallow", ["domains"], slug="backend/domains"),
            _doc("deep", ["domains/runs"], slug="backend/runs"),
        ],
        paths,
        target_min=1,
    )
    assert len(areas) == 2
    by_uids = {tuple(a["doc_uids"]): a for a in areas}
    assert by_uids[("deep",)]["scope_paths"] == ["domains/runs"]
    assert by_uids[("deep",)]["file_count"] == 3
    assert by_uids[("shallow",)]["scope_paths"] == ["domains/core", "domains/top.py"]
    assert by_uids[("shallow",)]["file_count"] == 3
    # Zero overlap between the emitted areas' matched file sets.
    matched = [_matched(a, paths) for a in areas]
    assert matched[0].isdisjoint(matched[1])
    assert matched[0] | matched[1] == set(paths)


def test_fully_shadowed_overview_page_produces_no_area():
    paths = [f"src/a/f{i}.py" for i in range(3)] + [f"src/b/f{i}.py" for i in range(3)]
    areas = normalize_areas(
        [
            _doc("overview", ["src"], slug="overview"),
            _doc("pa", ["src/a"], slug="src/a"),
            _doc("pb", ["src/b"], slug="src/b"),
        ],
        paths,
        target_min=1,
    )
    assert len(areas) == 2  # no remainder either — everything is owned
    assert all("overview" not in a["doc_uids"] for a in areas)
    matched = [_matched(a, paths) for a in areas]
    assert matched[0].isdisjoint(matched[1])
    assert matched[0] | matched[1] == set(paths)


def test_degraded_mode_still_dedups_identical_prefixes():
    # No tree: claim sizes are unknowable, so the identical prefix goes to
    # the lexicographically first slug; a fully deduped doc gets no area but
    # a doc with other prefixes keeps those.
    areas = normalize_areas(
        [
            _doc("dz", ["src", "other"], slug="zeta"),
            _doc("da", ["src"], slug="alpha"),
            _doc("db", ["lib"], slug="beta"),
        ],
        [],
    )
    assert all(a["file_count"] is None for a in areas)
    by_uids = {tuple(a["doc_uids"]): a for a in areas}
    assert by_uids[("da",)]["scope_paths"] == ["src"]
    assert by_uids[("dz",)]["scope_paths"] == ["other"]  # lost "src", kept "other"
    assert by_uids[("db",)]["scope_paths"] == ["lib"]
    assert len(areas) == 3


# ── filter_by_keys ──────────────────────────────────────────────────────────


def test_filter_by_keys_multi_select():
    areas = [
        {"area_key": "backend/delivery/convergence", "title": "a"},
        {"area_key": "backend/runs", "title": "b"},
        {"area_key": "frontend/views", "title": "c"},
        {"area_key": "", "title": "remainder"},
    ]
    from domains.campaigns.services import planner

    got = {a["title"] for a in planner.filter_by_keys(areas, ["backend/delivery", "frontend/views"])}
    assert got == {"a", "c"}
    assert len(planner.filter_by_keys(areas, [])) == 4  # empty = all


# ── build_plan_by_kind ───────────────────────────────────────────────────────


def _bpk_lens(key, *, enabled=True, global_agent_key=""):
    return {"key": key, "enabled": enabled, "global_agent_key": global_agent_key}


def _bpk_area(key, title=None, *, stale=False, scope_paths=None):
    k = key or "area"
    return {
        "area_key": key,
        "title": title or k,
        "scope_paths": scope_paths or [k],
        "doc_uids": [],
        "file_count": 10,
        "stale": stale,
    }


def _bpk_feature(title, *, stale=False):
    return {
        "title": title,
        "scope_paths": [title],
        "doc_uids": [],
        "file_count": 5,
        "stale": stale,
    }


# subsystem/all ----------------------------------------------------------------


def test_build_plan_by_kind_subsystem_all_one_part_per_area_all_enabled_lenses():
    areas = [_bpk_area("backend/a"), _bpk_area("backend/b")]
    lenses = [_bpk_lens("bugs"), _bpk_lens("security"), _bpk_lens("sec2", enabled=False)]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="all")
    assert [p["kind"] for p in parts] == ["area", "area"]
    assert parts[0]["lens_keys"] == ["bugs", "security"]
    assert parts[1]["lens_keys"] == ["bugs", "security"]


def test_build_plan_by_kind_subsystem_all_idx_sequential():
    areas = [_bpk_area("a"), _bpk_area("b"), _bpk_area("c")]
    lenses = [_bpk_lens("bugs")]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="all")
    assert [p["idx"] for p in parts] == [0, 1, 2]


def test_build_plan_by_kind_subsystem_all_no_area_no_parts():
    parts = build_plan_by_kind("subsystem", [], [_bpk_lens("bugs")], selection="all")
    assert parts == []


def test_build_plan_by_kind_subsystem_all_disabled_lens_excluded():
    areas = [_bpk_area("x")]
    lenses = [_bpk_lens("bugs", enabled=False)]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="all")
    assert parts[0]["lens_keys"] == []


# subsystem/stale --------------------------------------------------------------


def test_build_plan_by_kind_subsystem_stale_filters_to_stale_areas():
    areas = [_bpk_area("a", stale=False), _bpk_area("b", stale=True), _bpk_area("c", stale=True)]
    lenses = [_bpk_lens("bugs")]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="stale")
    assert len(parts) == 2
    assert {p["title"] for p in parts} == {"b", "c"}


def test_build_plan_by_kind_subsystem_stale_empty_when_none_stale():
    areas = [_bpk_area("a", stale=False)]
    parts = build_plan_by_kind("subsystem", areas, [_bpk_lens("bugs")], selection="stale")
    assert parts == []


# subsystem/rotation -----------------------------------------------------------


def test_build_plan_by_kind_subsystem_rotation_picks_k_least_recently_covered():
    recency = {
        "a0/f.py": NOW - timedelta(days=1),   # freshest
        "a1/f.py": NOW - timedelta(days=30),  # stalest covered
        # a2 never covered → comes first
    }
    areas = [
        _bpk_area("a0", scope_paths=["a0"]),
        _bpk_area("a1", scope_paths=["a1"]),
        _bpk_area("a2", scope_paths=["a2"]),
    ]
    lenses = [_bpk_lens("bugs")]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="rotation", k=2,
                               path_recency=recency)
    assert len(parts) == 2
    assert parts[0]["title"] == "a2"  # never covered → first
    assert parts[1]["title"] == "a1"  # stalest covered → second


def test_build_plan_by_kind_subsystem_rotation_without_recency_takes_first_k():
    areas = [_bpk_area(f"area-{i}") for i in range(5)]
    lenses = [_bpk_lens("bugs")]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="rotation", k=3)
    assert len(parts) == 3
    assert [p["title"] for p in parts] == ["area-0", "area-1", "area-2"]


def test_build_plan_by_kind_subsystem_rotation_k_larger_than_areas():
    areas = [_bpk_area("x"), _bpk_area("y")]
    parts = build_plan_by_kind("subsystem", areas, [_bpk_lens("bugs")], selection="rotation", k=10)
    assert len(parts) == 2


def test_build_plan_by_kind_subsystem_rotation_idx_sequential():
    areas = [_bpk_area(f"area-{i}") for i in range(3)]
    lenses = [_bpk_lens("bugs")]
    parts = build_plan_by_kind("subsystem", areas, lenses, selection="rotation", k=3)
    assert [p["idx"] for p in parts] == [0, 1, 2]


# feature/all ------------------------------------------------------------------


def test_build_plan_by_kind_feature_all_one_part_per_leaf():
    leaves = [_bpk_feature("Auth"), _bpk_feature("Search")]
    lenses = [_bpk_lens("implementation-gaps")]
    parts = build_plan_by_kind("feature", [], lenses, selection="all", feature_areas=leaves)
    assert [p["kind"] for p in parts] == ["feature", "feature"]
    assert {p["title"] for p in parts} == {"Auth", "Search"}


def test_build_plan_by_kind_feature_all_idx_sequential():
    leaves = [_bpk_feature(f"F{i}") for i in range(4)]
    parts = build_plan_by_kind("feature", [], [_bpk_lens("implementation-gaps")],
                               selection="all", feature_areas=leaves)
    assert [p["idx"] for p in parts] == [0, 1, 2, 3]


def test_build_plan_by_kind_feature_all_no_feature_areas_is_empty():
    parts = build_plan_by_kind("feature", [], [_bpk_lens("implementation-gaps")],
                               selection="all", feature_areas=None)
    assert parts == []


# feature/stale ----------------------------------------------------------------


def test_build_plan_by_kind_feature_stale_filters_to_stale_leaves():
    leaves = [_bpk_feature("A", stale=False), _bpk_feature("B", stale=True)]
    parts = build_plan_by_kind("feature", [], [_bpk_lens("implementation-gaps")],
                               selection="stale", feature_areas=leaves)
    assert len(parts) == 1
    assert parts[0]["title"] == "B"


def test_build_plan_by_kind_feature_rotation_same_as_stale():
    # rotation for feature falls back to stale semantics
    leaves = [_bpk_feature("X", stale=True), _bpk_feature("Y", stale=False)]
    parts = build_plan_by_kind("feature", [], [_bpk_lens("implementation-gaps")],
                               selection="rotation", feature_areas=leaves)
    assert len(parts) == 1
    assert parts[0]["title"] == "X"


def test_build_plan_by_kind_feature_lens_keys_from_passed_lenses():
    leaves = [_bpk_feature("Checkout")]
    lenses = [_bpk_lens("impl-gaps"), _bpk_lens("security")]
    parts = build_plan_by_kind("feature", [], lenses, selection="all", feature_areas=leaves)
    assert parts[0]["lens_keys"] == ["impl-gaps", "security"]


# global -----------------------------------------------------------------------


def test_build_plan_by_kind_global_one_part_per_enabled_lens():
    lenses = [
        _bpk_lens("arch", global_agent_key="arch"),
        _bpk_lens("sec-global", global_agent_key="sec-global"),
    ]
    parts = build_plan_by_kind("global", [], lenses, selection="all")
    assert [p["kind"] for p in parts] == ["global", "global"]
    assert {p["lens_keys"][0] for p in parts} == {"arch", "sec-global"}


def test_build_plan_by_kind_global_disabled_lens_excluded():
    lenses = [
        _bpk_lens("arch", global_agent_key="arch"),
        _bpk_lens("sec", global_agent_key="sec", enabled=False),
    ]
    parts = build_plan_by_kind("global", [], lenses, selection="all")
    assert len(parts) == 1
    assert parts[0]["lens_keys"] == ["arch"]


def test_build_plan_by_kind_global_idx_sequential():
    lenses = [_bpk_lens(f"g{i}", global_agent_key=f"g{i}") for i in range(3)]
    parts = build_plan_by_kind("global", [], lenses, selection="all")
    assert [p["idx"] for p in parts] == [0, 1, 2]


def test_build_plan_by_kind_global_part_title_contains_key():
    lenses = [_bpk_lens("architecture-review", global_agent_key="architecture-review")]
    parts = build_plan_by_kind("global", [], lenses, selection="all")
    assert "architecture-review" in parts[0]["title"]


# batch ------------------------------------------------------------------------


def test_build_plan_by_kind_batch_is_empty():
    assert build_plan_by_kind("batch", [], [], selection="all") == []


def test_build_plan_by_kind_batch_ignores_all_inputs():
    areas = [_bpk_area("x")]
    lenses = [_bpk_lens("bugs")]
    assert build_plan_by_kind("batch", areas, lenses, selection="all") == []
