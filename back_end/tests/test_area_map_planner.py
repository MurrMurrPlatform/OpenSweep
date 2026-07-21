"""Area-map planning — areas_from_map sizing, feature parts, prefix
slicing (planner pure functions + _plan_parts with its seams stubbed)."""

from types import SimpleNamespace

import pytest

from domains.campaigns.services import campaign_service
from domains.campaigns.services.planner import (
    REMAINDER_TITLE,
    areas_from_map,
    build_plan,
    filter_by_prefix,
)


def _leaf(area_key, scope, *, title="", doc_uids=None):
    return {
        "area_key": area_key,
        "title": title or area_key,
        "scope_paths": scope,
        "doc_uids": list(doc_uids or []),
    }


def _lens(key, *, scope="local", global_agent_key="", enabled=True):
    return {
        "key": key,
        "scope": scope,
        "global_agent_key": global_agent_key,
        "enabled": enabled,
    }


LENSES = [
    _lens("bugs"),
    _lens("architecture-review", scope="global", global_agent_key="architecture-review"),
]

TREE = (
    [f"be/api/f{i}.py" for i in range(3)]
    + [f"be/core/f{i}.py" for i in range(2)]
    + ["fe/app.ts", "vendor/lib.js", "README.md"]
)


# ── areas_from_map ───────────────────────────────────────────────────────────


def test_leaves_get_exclusive_counts_area_keys_and_a_remainder():
    areas, health = areas_from_map(
        [
            _leaf("backend/api", ["be/api"], doc_uids=["d1"]),
            _leaf("backend/core", ["be/core"]),
            _leaf("frontend", ["fe"], title="Frontend"),
        ],
        [],
        TREE,
    )
    by_key = {a["area_key"]: a for a in areas}
    assert by_key["backend/api"]["file_count"] == 3
    assert by_key["backend/api"]["doc_uids"] == ["d1"]
    assert by_key["backend/core"]["file_count"] == 2
    assert by_key["frontend"]["file_count"] == 1
    assert by_key["frontend"]["title"] == "Frontend"
    # Files no leaf covers sweep into the remainder (area_key "").
    remainder = by_key[""]
    assert remainder["title"] == REMAINDER_TITLE
    assert remainder["file_count"] == 2
    assert set(remainder["scope_paths"]) == {"vendor", "README.md"}
    # The counts partition the tree exactly — nothing double-audited.
    assert sum(a["file_count"] for a in areas) == len(TREE)
    assert health == {"overlapping_files": 0, "dead_ignore_scopes": []}
    assert all(a["dead_scope_paths"] == [] for a in areas)


def test_overlapping_leaves_and_dead_scopes_surface_in_health():
    areas, health = areas_from_map(
        [
            _leaf("backend", ["be", "attic"]),  # "attic" matches nothing
            _leaf("backend-api", ["be/api"]),  # claims files "backend" also claims
        ],
        ["ghost-vendor"],  # matches nothing → dead ignore scope
        TREE,
    )
    by_key = {a["area_key"]: a for a in areas}
    # be/api files are claimed by BOTH leaves — 3 double-claimed files.
    assert health["overlapping_files"] == 3
    assert health["dead_ignore_scopes"] == ["ghost-vendor"]
    assert by_key["backend"]["dead_scope_paths"] == ["attic"]
    assert by_key["backend-api"]["dead_scope_paths"] == []


def test_ignore_scopes_are_subtracted_from_leaves_and_the_remainder():
    areas, _health = areas_from_map(
        [_leaf("backend", ["be"]), _leaf("frontend", ["fe"])],
        ["vendor", "be/api"],
        TREE,
    )
    by_key = {a["area_key"]: a for a in areas}
    # A file under BOTH a leaf and an ignore scope drops out of the leaf
    # count — ignored files get no run scoped to them, anywhere.
    assert by_key["backend"]["file_count"] == 2  # be/core only; be/api fenced off
    remainder = by_key[""]
    assert remainder["title"] == REMAINDER_TITLE
    assert remainder["file_count"] == 1  # README.md; vendor + be/api fenced off
    assert "vendor" not in remainder["scope_paths"]
    assert "be" not in remainder["scope_paths"]


def test_oversized_leaf_is_flagged_never_split():
    paths = [f"big/a/f{i}.py" for i in range(4)] + [f"big/b/f{i}.py" for i in range(4)]
    areas, _health = areas_from_map([_leaf("big", ["big"])], [], paths, target_max=5)
    assert len(areas) == 1  # semantic sizing is the mapping agent's job
    assert areas[0]["oversized"] is True
    assert areas[0]["file_count"] == 8
    assert areas[0]["scope_paths"] == ["big"]


def test_tiny_leaves_never_merge():
    areas, _health = areas_from_map(
        [_leaf("a", ["a"]), _leaf("b", ["b"])], [], ["a/f.py", "b/f.py"]
    )
    assert [a["area_key"] for a in areas] == ["a", "b"]


def test_empty_tree_passes_leaves_through_uncounted():
    areas, health = areas_from_map([_leaf("backend", ["be"])], [], [])
    assert len(areas) == 1  # no remainder can exist without a tree
    assert areas[0]["file_count"] is None
    assert areas[0]["oversized"] is False
    assert areas[0]["area_key"] == "backend"
    # No tree ⇒ no health claims either: nothing is declared dead.
    assert areas[0]["dead_scope_paths"] == []
    assert health == {"overlapping_files": 0, "dead_ignore_scopes": []}


def test_oversized_remainder_still_splits_by_subdir():
    paths = (
        [f"x/f{i}.py" for i in range(100)]
        + [f"y/f{i}.py" for i in range(100)]
        + ["fe/app.ts"]
    )
    areas, _health = areas_from_map([_leaf("frontend", ["fe"])], [], paths, target_max=150)
    remainders = [a for a in areas if a["title"].startswith(REMAINDER_TITLE)]
    assert len(remainders) == 2
    assert {tuple(a["scope_paths"]) for a in remainders} == {("x",), ("y",)}
    assert all(a["area_key"] == "" for a in remainders)


def test_oversized_remainder_tiny_pieces_still_merge():
    paths = [f"z{i}/f{j}.py" for i in range(6) for j in range(10)]
    areas, _health = areas_from_map([], [], paths, target_max=50)
    assert len(areas) == 2  # z0..z4 merged to 50 files, z5 left over
    assert " + " in areas[0]["title"]
    assert areas[0]["file_count"] == 50
    assert all(a["area_key"] == "" for a in areas)


# ── build_plan: feature parts ────────────────────────────────────────────────


def _map_area(key, scope):
    return {
        "title": key,
        "scope_paths": scope,
        "doc_uids": [],
        "file_count": 10,
        "area_key": key,
        "oversized": False,
    }


FEATURE = {
    "title": "Checkout",
    "scope_paths": ["be/checkout"],
    "doc_uids": ["d9"],
    "file_count": 4,
    "area_key": "features/checkout",
}


def test_full_plan_appends_feature_parts_between_areas_and_globals():
    parts = build_plan(
        "full", [_map_area("backend", ["be"])], LENSES, feature_areas=[FEATURE]
    )
    assert [p["kind"] for p in parts] == ["area", "feature", "global"]
    assert [p["idx"] for p in parts] == [0, 1, 2]
    feat = parts[1]
    assert feat["lens_keys"] == ["implementation-gaps"]
    assert feat["title"] == "Checkout"
    assert feat["area_keys"] == ["features/checkout"]
    assert feat["scope_paths"] == ["be/checkout"]
    assert feat["doc_uids"] == ["d9"]
    assert feat["file_count"] == 4
    assert feat["state"] == "pending" and feat["run_uid"] == ""


def test_rotation_and_focused_never_emit_feature_parts():
    areas = [_map_area("backend", ["be"])]
    plans = [
        build_plan("rotation", areas, LENSES, k=3, feature_areas=[FEATURE]),
        build_plan("focused", areas, LENSES, focus_lens="bugs", feature_areas=[FEATURE]),
    ]
    for parts in plans:
        assert all(p["kind"] != "feature" for p in parts)


def test_area_parts_carry_their_area_keys():
    parts = build_plan("full", [_map_area("backend", ["be"])], [_lens("bugs")])
    assert parts[0]["area_keys"] == ["backend"]


def test_bundled_areas_carry_all_their_keys():
    bundle = {
        "title": "Backend — A + B",
        "scope_paths": ["be/a", "be/b"],
        "doc_uids": [],
        "file_count": 40,
        "area_keys": ["backend/a", "backend/b"],
        "oversized": False,
    }
    parts = build_plan("full", [bundle], [_lens("bugs")])
    assert parts[0]["area_keys"] == ["backend/a", "backend/b"]


def test_docs_derived_areas_get_empty_area_keys():
    docs_area = {"title": "t", "scope_paths": ["s"], "doc_uids": [], "file_count": 1}
    parts = build_plan("full", [docs_area], [_lens("bugs")])
    assert parts[0]["area_keys"] == []


# ── filter_by_prefix ─────────────────────────────────────────────────────────


def test_filter_by_prefix_respects_the_key_boundary():
    areas = [
        {"area_key": "backend", "title": "b"},
        {"area_key": "backend/api", "title": "ba"},
        {"area_key": "backend-jobs", "title": "bj"},  # NOT under "backend"
        {"area_key": "", "title": "remainder"},
    ]
    picked = filter_by_prefix(areas, "backend")
    assert [a["area_key"] for a in picked] == ["backend", "backend/api"]


def test_filter_by_empty_prefix_keeps_everything():
    areas = [{"area_key": "backend", "title": "b"}, {"area_key": "", "title": "r"}]
    assert filter_by_prefix(areas, "") == areas


def test_empty_key_areas_survive_only_the_empty_prefix():
    areas = [{"area_key": "", "title": "docs-derived"}]
    assert filter_by_prefix(areas, "anything") == []


# ── _plan_parts: prefix slicing + scope_hint decoration ──────────────────────


@pytest.fixture
def plan_seams(monkeypatch):
    """Repository lookup, _plan_areas, and the lens catalog stubbed;
    build_plan + filter_by_prefix run for real."""
    import domains.lenses.services.lens_service as lens_service_mod
    import domains.repositories.models as repo_models

    state = SimpleNamespace(areas=[], features=[], source="area-map", degraded="")

    class _Nodes:
        @staticmethod
        async def get_or_none(**kw):
            return SimpleNamespace(uid="repo1") if kw.get("uid") == "repo1" else None

    monkeypatch.setattr(repo_models, "Repository", SimpleNamespace(nodes=_Nodes))

    async def fake_plan_areas(repository_uid, repo):
        return (
            state.areas,
            state.degraded,
            10,
            state.source,
            state.features,
            {"overlapping_files": 0, "dead_ignore_scopes": []},
        )

    monkeypatch.setattr(campaign_service, "_plan_areas", fake_plan_areas)

    async def fake_list_lenses(enabled_only=True):
        return [
            SimpleNamespace(key="bugs", scope="local", global_agent_key="", enabled=True),
            SimpleNamespace(
                key="architecture-review",
                scope="global",
                global_agent_key="architecture-review",
                enabled=True,
            ),
        ]

    monkeypatch.setattr(lens_service_mod, "list_lenses", fake_list_lenses)
    return state


async def test_area_prefix_slices_the_plan_and_decorates_globals(plan_seams):
    plan_seams.areas = [
        _map_area("backend/api", ["be/api"]),
        _map_area("backend/core", ["be/core"]),
        _map_area("frontend", ["fe"]),
    ]
    plan_seams.features = [
        {**FEATURE, "area_key": "backend/api/checkout"},
        {**FEATURE, "title": "Theming", "area_key": "frontend/theming"},
    ]
    parts, degraded, source = await campaign_service._plan_parts(
        "repo1", template="full", lens_keys=[], k=3, area_prefix="backend"
    )
    assert source == "area-map" and degraded == ""
    # The two undersized backend siblings (10 files each) bundle into one
    # area part carrying both keys.
    assert [p["area_keys"] for p in parts if p["kind"] == "area"] == [
        ["backend/api", "backend/core"]
    ]
    assert [p["area_keys"] for p in parts if p["kind"] == "feature"] == [
        ["backend/api/checkout"]
    ]
    globals_ = [p for p in parts if p["kind"] == "global"]
    assert len(globals_) == 1
    # The global sweep is steered to the slice's union scope.
    assert globals_[0]["scope_hint"] == ["be/api", "be/core"]


async def test_prefix_that_matches_nothing_is_legal_but_noted(plan_seams):
    plan_seams.areas = [_map_area("backend", ["be"])]
    parts, degraded, source = await campaign_service._plan_parts(
        "repo1", template="full", lens_keys=[], k=3, area_prefix="nope"
    )
    assert source == "area-map"
    assert [p["kind"] for p in parts] == ["global"]  # zero area parts is legal
    assert "area_prefix 'nope' matched no areas" in degraded


async def test_no_prefix_leaves_global_parts_undecorated(plan_seams):
    plan_seams.areas = [_map_area("backend", ["be"])]
    parts, _degraded, _source = await campaign_service._plan_parts(
        "repo1", template="full", lens_keys=[], k=3
    )
    assert all("scope_hint" not in p for p in parts)
