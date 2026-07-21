"""Campaign planner — pure partition + template math."""

from datetime import UTC, datetime, timedelta

from domains.campaigns.services.planner import build_plan, normalize_areas

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
    from domains.docs.services.doc_freshness import watches_path

    return {p for p in paths if watches_path(area["scope_paths"], p)}


def _lens(key, *, scope="local", global_agent_key="", enabled=True):
    return {
        "key": key,
        "scope": scope,
        "global_agent_key": global_agent_key,
        "enabled": enabled,
    }


LENSES = [
    _lens("bugs"),
    _lens("security"),
    _lens("architecture-review", scope="global", global_agent_key="architecture-review"),
]


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


# ── build_plan templates ─────────────────────────────────────────────────────


def _areas(n):
    return [
        {"title": f"a{i}", "scope_paths": [f"a{i}"], "doc_uids": [f"d{i}"], "file_count": 60}
        for i in range(n)
    ]


def test_full_plan_covers_all_areas_plus_globals():
    parts = build_plan("full", _areas(3), LENSES)
    areas = [p for p in parts if p["kind"] == "area"]
    globals_ = [p for p in parts if p["kind"] == "global"]
    assert len(areas) == 3 and len(globals_) == 1
    assert all(p["lens_keys"] == ["bugs", "security"] for p in areas)
    assert globals_[0]["lens_keys"] == ["architecture-review"]
    # idx sequential, areas before globals, all pending.
    assert [p["idx"] for p in parts] == list(range(4))
    assert all(p["state"] == "pending" and p["run_uid"] == "" for p in parts)
    # Docs-derived areas carry no area key — parts get the [] default.
    assert all(p["area_keys"] == [] for p in parts)


def test_disabled_lenses_are_excluded():
    lenses = [_lens("bugs"), _lens("security", enabled=False)]
    parts = build_plan("full", _areas(1), lenses)
    assert parts[0]["lens_keys"] == ["bugs"]


def test_rotation_picks_least_recently_covered_never_covered_first():
    recency = {
        "a0/f.py": NOW - timedelta(days=1),  # freshest
        "a1/f.py": NOW - timedelta(days=30),  # stalest covered
        # a2 never covered
    }
    parts = build_plan("rotation", _areas(3), LENSES, k=2, path_recency=recency)
    assert [p["title"] for p in parts] == ["a2", "a1"]
    assert all(p["kind"] == "area" for p in parts)  # no globals in rotation


def test_rotation_without_recency_takes_first_k():
    parts = build_plan("rotation", _areas(5), LENSES, k=3)
    assert [p["title"] for p in parts] == ["a0", "a1", "a2"]


def test_focused_uses_only_the_focus_lens():
    parts = build_plan("focused", _areas(2), LENSES, focus_lens="security")
    assert all(p["lens_keys"] == ["security"] for p in parts)
    assert all(p["kind"] == "area" for p in parts)  # security has no global part


def test_focused_on_a_global_lens_adds_its_sweep():
    parts = build_plan("focused", _areas(2), LENSES, focus_lens="architecture-review")
    globals_ = [p for p in parts if p["kind"] == "global"]
    assert len(globals_) == 1
    assert globals_[0]["lens_keys"] == ["architecture-review"]
    assert globals_[0]["idx"] == 2  # after the areas
