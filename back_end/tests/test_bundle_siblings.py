"""bundle_siblings — pure sibling-leaf bundling for area-map plans.

Small sibling leaves (same parent key prefix) merge into one part so a
fine-grained map still yields runs worth dispatching; adequate leaves,
unsized leaves (degraded tree), and remainder areas always stand alone.
"""

from datetime import UTC, datetime, timedelta

from domains.campaigns.services.planner import build_plan, bundle_siblings

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _leaf(key, count, *, title="", scope=None, doc_uids=None, oversized=False):
    return {
        "area_key": key,
        "title": title or key,
        "scope_paths": list(scope or [key.replace("/", "_")]),
        "doc_uids": list(doc_uids or []),
        "file_count": count,
        "oversized": oversized,
    }


# ── the band ─────────────────────────────────────────────────────────────────


def test_tiny_siblings_merge_and_flush_at_target_min():
    out = bundle_siblings(
        [
            _leaf("be/a", 20),
            _leaf("be/b", 20),
            _leaf("be/c", 20),
            _leaf("be/d", 20),
        ],
        target_min=50,
        target_max=150,
    )
    # a+b+c reaches 60 ≥ 50 and flushes; d is left as its own (partial) part.
    assert [b["area_keys"] for b in out] == [["be/a", "be/b", "be/c"], ["be/d"]]
    assert out[0]["file_count"] == 60
    assert out[1]["file_count"] == 20


def test_adequate_areas_always_stand_alone():
    out = bundle_siblings(
        [_leaf("be/big", 80), _leaf("be/huge", 200, oversized=True)],
        target_min=50,
        target_max=150,
    )
    assert [b["area_keys"] for b in out] == [["be/big"], ["be/huge"]]
    # Standalone leaves keep their own shape — title and oversized flag.
    assert out[0]["title"] == "be/big"
    assert out[1]["oversized"] is True


def test_a_merge_never_pushes_a_bundle_past_target_max():
    # target_min high enough that naive merging would exceed target_max:
    # the buffer flushes early instead.
    out = bundle_siblings(
        [_leaf("be/a", 90), _leaf("be/b", 90)], target_min=100, target_max=150
    )
    assert [b["area_keys"] for b in out] == [["be/a"], ["be/b"]]


# ── boundaries ───────────────────────────────────────────────────────────────


def test_parent_group_boundary_is_never_crossed():
    out = bundle_siblings(
        [
            _leaf("backend/api", 10),
            _leaf("backend/core", 10),
            _leaf("frontend/views", 10),
            _leaf("frontend/state", 10),
        ]
    )
    assert [b["area_keys"] for b in out] == [
        ["backend/api", "backend/core"],
        ["frontend/views", "frontend/state"],
    ]


def test_top_level_keys_group_under_the_root():
    out = bundle_siblings([_leaf("auth", 10), _leaf("billing", 10)])
    assert [b["area_keys"] for b in out] == [["auth", "billing"]]


def test_none_file_count_is_never_bundled():
    out = bundle_siblings(
        [_leaf("be/a", None), _leaf("be/b", 10), _leaf("be/c", 10)]
    )
    assert [b["area_keys"] for b in out] == [["be/a"], ["be/b", "be/c"]]
    assert out[0]["file_count"] is None


def test_remainder_areas_pass_through_with_no_keys():
    remainder = {
        "area_key": "",
        "title": "Uncovered paths",
        "scope_paths": ["scripts"],
        "doc_uids": [],
        "file_count": 3,
        "oversized": False,
    }
    out = bundle_siblings([remainder, _leaf("be/a", 10)])
    assert out[0]["area_keys"] == []
    assert out[0]["title"] == "Uncovered paths"
    assert out[1]["area_keys"] == ["be/a"]
    # NO COMPAT: the singular key never survives into a bundle dict.
    assert all("area_key" not in b for b in out)


# ── bundle shape ─────────────────────────────────────────────────────────────


def test_bundle_titles_and_unions():
    out = bundle_siblings(
        [
            _leaf(
                "backend/domains/delivery",
                10,
                title="Delivery",
                scope=["be/domains/delivery"],
                doc_uids=["d1", "d2"],
            ),
            _leaf(
                "backend/domains/checked",
                10,
                title="Checked",
                scope=["be/domains/checked", "be/domains/delivery"],
                doc_uids=["d2", "d3"],
            ),
        ],
        target_min=15,
    )
    (bundle,) = out
    assert bundle["area_keys"] == [
        "backend/domains/delivery",
        "backend/domains/checked",
    ]
    # Parent prefix, title-cased, then the leaf titles joined.
    assert bundle["title"] == "Backend/Domains — Delivery + Checked"
    assert bundle["scope_paths"] == [
        "be/domains/delivery",
        "be/domains/checked",
    ]  # union, deduped, order preserved
    assert bundle["doc_uids"] == ["d1", "d2", "d3"]
    assert bundle["file_count"] == 20
    assert bundle["oversized"] is False


def test_top_level_bundle_titles_from_the_first_segment():
    out = bundle_siblings(
        [_leaf("auth-service", 10, title="Auth"), _leaf("billing", 10, title="Billing")]
    )
    (bundle,) = out
    assert bundle["title"] == "Auth Service — Auth + Billing"


# ── downstream: rotation + docs-derived parts ────────────────────────────────


def test_rotation_ranks_bundles_by_their_stalest_union_path():
    bundles = bundle_siblings(
        [
            _leaf("be/fresh", 10, scope=["fresh"]),
            _leaf("be/stale", 10, scope=["stale"]),
            _leaf("fe/never", 10, scope=["never"]),
        ]
    )
    # be/fresh + be/stale bundled; fe/never stands alone (own parent group).
    assert [b["area_keys"] for b in bundles] == [
        ["be/fresh", "be/stale"],
        ["fe/never"],
    ]
    recency = {
        "fresh/f.py": NOW - timedelta(days=1),
        "stale/f.py": NOW - timedelta(days=30),
        # "never" was never covered → the fe bundle ranks first.
    }
    parts = build_plan(
        "rotation",
        bundles,
        [{"key": "bugs", "scope": "local", "global_agent_key": "", "enabled": True}],
        k=2,
        path_recency=recency,
    )
    # Never-covered first; then the bundle scored by its STALEST union path.
    assert [p["area_keys"] for p in parts] == [
        ["fe/never"],
        ["be/fresh", "be/stale"],
    ]


def test_docs_derived_parts_have_empty_area_keys():
    docs_area = {"title": "API", "scope_paths": ["src"], "doc_uids": ["d1"], "file_count": 9}
    parts = build_plan(
        "full",
        [docs_area],
        [{"key": "bugs", "scope": "local", "global_agent_key": "", "enabled": True}],
    )
    assert parts[0]["area_keys"] == []
