"""build_summary — the pure campaign digest."""

from domains.campaigns.services.finalize import build_summary


def _part(idx, *, state="done", covered=0, skipped=0, title=""):
    return {
        "idx": idx,
        "kind": "area",
        "title": title or f"p{idx}",
        "scope_paths": [f"p{idx}"],
        "state": state,
        "covered": covered,
        "skipped": skipped,
    }


def test_counts_by_severity_and_part():
    parts = [_part(0), _part(1)]
    findings = {
        0: [
            {"severity": "high", "tags": [], "title": "a"},
            {"severity": "low", "tags": [], "title": "b"},
        ],
        1: [{"severity": "high", "tags": [], "title": "c"}],
    }
    summary = build_summary(parts, findings, [])
    assert summary["counts"]["total"] == 3
    assert summary["counts"]["by_severity"] == {"high": 2, "low": 1}
    assert summary["counts"]["by_part"] == {"0": 2, "1": 1}


def test_coverage_rows_carry_part_state_and_counts():
    parts = [_part(0, covered=12, skipped=3), _part(1, state="failed")]
    summary = build_summary(parts, {}, ["p1"])
    rows = summary["coverage"]["parts"]
    assert rows[0] == {
        "idx": 0,
        "title": "p0",
        "covered": 12,
        "skipped": 3,
        # How many paths the run was OBSERVED to open, and which of claim /
        # measurement / dispatch-scope the row rests on. A part whose stamp
        # carried neither reads "unknown", never a confident zero.
        "observed": 0,
        "coverage_source": "unknown",
        "state": "done",
    }
    assert rows[1]["state"] == "failed"
    assert summary["coverage"]["holes"] == ["p1"]


def test_failed_parts_listed_sorted():
    parts = [_part(2, state="failed"), _part(0, state="failed"), _part(1)]
    summary = build_summary(parts, {}, [])
    assert summary["failed_parts"] == [0, 2]


def test_missing_severity_defaults_to_medium():
    summary = build_summary([_part(0)], {0: [{"tags": [], "title": "x"}]}, [])
    assert summary["counts"]["by_severity"] == {"medium": 1}


def _feature_part(idx, area_key, *, state="done", covered=0, skipped=0, title=""):
    return {
        "idx": idx,
        "kind": "feature",
        "title": title or area_key,
        "scope_paths": [area_key],
        "state": state,
        "covered": covered,
        "skipped": skipped,
        "area_keys": [area_key],
    }


def test_feature_rollup_aggregates_sub_feature_leaves_to_their_parent():
    # Two sub-feature leaves under "features/checkout" roll up to that parent;
    # "features/search" (its own leaf under "features") rolls up under
    # "features" — the key minus its last segment.
    parts = [
        _feature_part(0, "features/checkout/pay", covered=5, skipped=1),
        _feature_part(1, "features/checkout/refund", covered=3, state="failed"),
        _feature_part(2, "features/search", covered=2),
    ]
    findings = {0: [{"severity": "high", "tags": [], "title": "a"}]}
    rollup = build_summary(parts, findings, [])["coverage"]["feature_rollup"]
    by_key = {g["feature_key"]: g for g in rollup}
    checkout = by_key["features/checkout"]
    assert checkout["leaf_count"] == 2
    assert checkout["covered"] == 8 and checkout["skipped"] == 1
    assert checkout["findings"] == 1
    assert checkout["state"] == "partial"  # one leaf done, one failed
    assert {leaf["area_key"] for leaf in checkout["leaves"]} == {
        "features/checkout/pay",
        "features/checkout/refund",
    }
    search = by_key["features"]
    assert search["leaf_count"] == 1 and search["state"] == "covered"


def test_top_level_feature_leaf_rolls_up_under_itself():
    # A leaf with NO "/" separator rolls up under its own key.
    parts = [_feature_part(0, "checkout", covered=4)]
    rollup = build_summary(parts, {}, [])["coverage"]["feature_rollup"]
    assert rollup[0]["feature_key"] == "checkout"
    assert rollup[0]["leaf_count"] == 1


def test_feature_rollup_ignores_non_feature_parts():
    summary = build_summary([_part(0), _part(1)], {}, [])
    assert summary["coverage"]["feature_rollup"] == []


def test_empty_campaign_summary_shape():
    summary = build_summary([], {}, [])
    assert summary["counts"] == {"by_severity": {}, "by_part": {}, "total": 0}
    assert summary["coverage"] == {
        "parts": [],
        "holes": [],
        "feature_rollup": [],
        "lens_rollup": [],
    }
    assert summary["failed_parts"] == []


# ── Lens rollup ───────────────────────────────────────────────────────────


def _lens_part(idx, lens_keys, verdicts=None, **kw):
    part = _part(idx, **kw)
    part["lens_keys"] = list(lens_keys)
    part["lens_verdicts"] = list(verdicts or [])
    return part


def test_lens_rollup_tallies_each_lens_across_parts():
    """The digest reported finding counts and dropped the per-lens verdicts
    the coverage contract was collecting — "performance skipped in 2 of 3
    parts" is the honest coverage statement it should have been making."""
    parts = [
        _lens_part(
            0,
            ["bugs", "performance"],
            [
                {"lens": "bugs", "verdict": "checked-findings"},
                {"lens": "performance", "verdict": "skipped"},
            ],
        ),
        _lens_part(
            1,
            ["bugs", "performance"],
            [
                {"lens": "bugs", "verdict": "checked-clean"},
                {"lens": "performance", "verdict": "skipped"},
            ],
        ),
    ]

    rollup = build_summary(parts, {}, [])["coverage"]["lens_rollup"]

    by_lens = {row["lens"]: row for row in rollup}
    assert by_lens["bugs"]["checked-findings"] == 1
    assert by_lens["bugs"]["checked-clean"] == 1
    assert by_lens["performance"]["skipped"] == 2
    assert by_lens["performance"]["parts"] == 2


def test_a_lens_with_no_verdict_is_missing_not_skipped():
    """An agent saying "I skipped performance" and an agent silently omitting
    it are different failures — only the first is the contract working."""
    parts = [_lens_part(0, ["bugs"], [])]

    rollup = build_summary(parts, {}, [])["coverage"]["lens_rollup"]

    assert rollup[0]["missing"] == 1
    assert rollup[0]["skipped"] == 0


def test_an_unrecognised_verdict_counts_as_missing_rather_than_crashing():
    parts = [_lens_part(0, ["bugs"], [{"lens": "bugs", "verdict": "vibes"}])]
    rollup = build_summary(parts, {}, [])["coverage"]["lens_rollup"]
    assert rollup[0]["missing"] == 1


def test_parts_without_lenses_contribute_nothing():
    assert build_summary([_part(0)], {}, [])["coverage"]["lens_rollup"] == []
