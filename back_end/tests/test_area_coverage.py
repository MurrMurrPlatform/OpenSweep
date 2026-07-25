"""Pure tests for axis_coverage — "does each axis actually partition the repo?"

The bug these pin: the map has TWO axes and nothing ever checked either one
for GAPS. A run produced a complete subsystem partition plus five feature
areas covering one directory and called itself done, so the axes are computed
— and asserted here — independently.
"""

from domains.areas.services.area_coverage import (
    MAX_REPORTED_PATHS,
    axis_coverage,
    axis_of_kind,
    gap_sentence,
    incomplete_axes,
)


def _area(key, kind="subsystem", scope_paths=(), enabled=True):
    return {
        "key": key,
        "kind": kind,
        "scope_paths": list(scope_paths),
        "enabled": enabled,
    }


# ── axis membership ─────────────────────────────────────────────────────────


def test_kinds_map_to_their_axis():
    assert axis_of_kind("subsystem") == "subsystem"
    assert axis_of_kind("ignore") == "subsystem"
    assert axis_of_kind("feature") == "feature"
    assert axis_of_kind("feature_ignore") == "feature"
    assert axis_of_kind("nonsense") == ""
    assert axis_of_kind("") == ""


# ── the basic partition questions ───────────────────────────────────────────


def test_both_axes_complete_when_every_file_is_claimed_once():
    files = ["back_end/app.py", "front_end/main.ts", "uv.lock"]
    areas = [
        _area("backend", scope_paths=["back_end"]),
        _area("frontend", scope_paths=["front_end"]),
        _area("locks", kind="ignore", scope_paths=["uv.lock"]),
        _area("features/checkout", kind="feature", scope_paths=["back_end", "front_end"]),
        _area("features/none", kind="feature_ignore", scope_paths=["uv.lock"]),
    ]
    cov = axis_coverage(files, areas)

    assert cov["subsystem"]["complete"] is True
    assert cov["feature"]["complete"] is True
    assert cov["subsystem"]["uncovered_total"] == 0
    assert cov["feature"]["uncovered_total"] == 0
    assert incomplete_axes(cov) == []


def test_a_gap_is_reported_with_the_missing_paths():
    files = ["back_end/app.py", "scripts/deploy.sh", "docs/readme.md"]
    areas = [_area("backend", scope_paths=["back_end"])]
    cov = axis_coverage(files, areas)["subsystem"]

    assert cov["complete"] is False
    assert cov["uncovered"] == ["docs/readme.md", "scripts/deploy.sh"]  # sorted
    assert cov["uncovered_total"] == 2
    assert cov["tracked_total"] == 3


def test_no_areas_at_all_leaves_the_whole_tree_uncovered():
    cov = axis_coverage(["a.py", "b.py"], [])
    assert cov["subsystem"]["uncovered_total"] == 2
    assert cov["feature"]["uncovered_total"] == 2
    assert incomplete_axes(cov) == ["subsystem", "feature"]


def test_overlapping_files_name_the_colliding_leaves():
    files = ["back_end/api/v1.py"]
    areas = [
        _area("backend/api", scope_paths=["back_end/api"]),
        _area("backend/v1", scope_paths=["back_end/api/v1.py"]),
    ]
    cov = axis_coverage(files, areas)["subsystem"]

    assert cov["overlapping"] == [
        {"path": "back_end/api/v1.py", "keys": ["backend/api", "backend/v1"]}
    ]
    assert cov["overlapping_total"] == 1
    # A collision is a conflict to resolve, not a hole — `complete` tracks gaps.
    assert cov["complete"] is True


def test_scope_matching_respects_the_slash_boundary():
    # "back_end" must not claim "back_end2/…" — the same rule _paths_overlap uses.
    cov = axis_coverage(["back_end2/app.py"], [_area("backend", scope_paths=["back_end"])])
    assert cov["subsystem"]["uncovered"] == ["back_end2/app.py"]


def test_a_file_scope_covers_exactly_that_file():
    cov = axis_coverage(
        ["uv.lock", "uv.lockfile"], [_area("locks", kind="ignore", scope_paths=["uv.lock"])]
    )
    assert cov["subsystem"]["uncovered"] == ["uv.lockfile"]


# ── what does and does not count as a leaf ──────────────────────────────────


def test_scopeless_parents_contribute_no_coverage():
    """A grouping key ("features", "backend") owns no files by design — if it
    counted, an empty parent would silently "cover" nothing and still make the
    axis look staffed."""
    files = ["back_end/app.py"]
    cov = axis_coverage(files, [_area("backend", scope_paths=[])])

    assert cov["subsystem"]["leaves"] == 0
    assert cov["subsystem"]["uncovered"] == ["back_end/app.py"]


def test_disabled_areas_are_out_of_the_partition():
    files = ["back_end/app.py"]
    areas = [_area("backend", scope_paths=["back_end"], enabled=False)]
    cov = axis_coverage(files, areas)["subsystem"]

    assert cov["leaves"] == 0
    assert cov["uncovered"] == ["back_end/app.py"]


def test_areas_of_the_other_axis_never_cover_this_one():
    files = ["back_end/app.py"]
    areas = [_area("features/checkout", kind="feature", scope_paths=["back_end"])]
    cov = axis_coverage(files, areas)

    assert cov["feature"]["complete"] is True
    assert cov["subsystem"]["uncovered"] == ["back_end/app.py"]


def test_feature_ignore_counts_as_feature_axis_coverage():
    """The whole point of the new kind: lockfiles and generated assets belong
    to no product feature, and without a place to put them the feature axis can
    never be closed."""
    files = ["uv.lock", "front_end/dist/bundle.js"]
    areas = [
        _area("features/none", kind="feature_ignore", scope_paths=["uv.lock", "front_end/dist"])
    ]
    cov = axis_coverage(files, areas)["feature"]

    assert cov["complete"] is True
    assert cov["uncovered_total"] == 0


# ── the production failure, in miniature and at full size ───────────────────


def test_a_complete_subsystem_axis_does_not_complete_the_feature_axis():
    """THE regression. Two axes, computed independently: a finished subsystem
    partition must never make a half-finished feature axis look done."""
    files = ["back_end/app.py", "back_end/domains/areas/svc.py", "front_end/main.ts"]
    areas = [
        _area("backend", scope_paths=["back_end"]),
        _area("frontend", scope_paths=["front_end"]),
        _area("features/areas", kind="feature", scope_paths=["back_end/domains/areas"]),
    ]
    cov = axis_coverage(files, areas)

    assert cov["subsystem"]["complete"] is True
    assert cov["feature"]["complete"] is False
    assert cov["feature"]["uncovered"] == ["back_end/app.py", "front_end/main.ts"]
    assert incomplete_axes(cov) == ["feature"]


def _production_shape():
    """~24 subsystem/ignore areas tiling the tree + 5 feature areas covering a
    single feature family — the shape of the run that shipped a half-map."""
    files = [f"back_end/domains/d{i}/svc.py" for i in range(20)]
    files += [f"front_end/src/views/V{i}.ts" for i in range(10)]
    files += ["uv.lock", "package-lock.json"]

    areas = [_area(f"backend/d{i}", scope_paths=[f"back_end/domains/d{i}"]) for i in range(20)]
    areas += [_area(f"frontend/v{i}", scope_paths=[f"front_end/src/views/V{i}.ts"]) for i in range(10)]
    areas += [_area("vendor", kind="ignore", scope_paths=["uv.lock", "package-lock.json"])]
    areas += [_area("features", kind="feature", scope_paths=[])]  # scopeless charter
    areas += [
        _area(
            f"features/delivery/stage{i}",
            kind="feature",
            scope_paths=[f"back_end/domains/d{i}"],
        )
        for i in range(5)
    ]
    return files, areas


def test_production_shape_subsystem_complete_feature_incomplete():
    files, areas = _production_shape()
    cov = axis_coverage(files, areas)

    assert cov["subsystem"]["complete"] is True
    assert cov["feature"]["complete"] is False
    # 32 tracked files, 5 claimed by feature leaves → 27 orphans on that axis.
    assert cov["feature"]["uncovered_total"] == 27
    assert cov["feature"]["leaves"] == 5  # the scopeless "features" parent is not one


# ── output budget ───────────────────────────────────────────────────────────


def test_lists_are_capped_but_totals_are_always_true():
    files = [f"src/f{i:04d}.py" for i in range(MAX_REPORTED_PATHS + 50)]
    cov = axis_coverage(files, [])["subsystem"]

    assert len(cov["uncovered"]) == MAX_REPORTED_PATHS
    assert cov["uncovered_total"] == MAX_REPORTED_PATHS + 50


def test_cap_is_overridable():
    cov = axis_coverage(["a.py", "b.py", "c.py"], [], cap=1)["subsystem"]
    assert cov["uncovered"] == ["a.py"] and cov["uncovered_total"] == 3


# ── the shared gap sentence ─────────────────────────────────────────────────


def test_gap_sentence_names_the_axis_the_total_and_a_sample():
    cov = axis_coverage(["a.py", "b.py"], [])
    line = gap_sentence("feature", cov["feature"])

    assert line.startswith("feature axis incomplete: 2 of 2 tracked files")
    assert "a.py" in line and "b.py" in line


def test_gap_sentence_counts_the_paths_it_did_not_name():
    cov = axis_coverage([f"f{i}.py" for i in range(20)], [])
    line = gap_sentence("subsystem", cov["subsystem"], sample=3)

    assert "(+17 more)" in line


def test_gap_sentence_is_empty_for_a_complete_axis():
    cov = axis_coverage(["a.py"], [_area("all", scope_paths=["a.py"])])
    assert gap_sentence("subsystem", cov["subsystem"]) == ""
