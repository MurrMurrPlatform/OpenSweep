"""Lens grouping, and the FORCE re-seed that had no caller.

A default subsystem part carries ALL EIGHT lenses over up to 150 files at
`normal` effort — 200 tool turns for eight disciplines. Splitting it is the
obvious fix and also a 3x run-count increase with no data behind it, so it
ships opt-in with the instrument that settles it (the digest's per-lens
rollup) rather than as a guess imposed on everyone.

SeedMode.FORCE existed as an enum value whose only callers were tests. Boot
runs SYNC, which rolls a shipped improvement forward only onto rows nobody
edited — right as a default, and it left no way to say "yes, take mine". That
cost two deploys.
"""

import pytest

from domains.campaigns.services.planner import build_plan_by_kind
from domains.lenses.services.lens_service import LENS_GROUPS, group_lens_keys


def _lens(key):
    return {"key": key, "global_agent_key": "", "enabled": True}


def _area(key):
    return {
        "area_key": key,
        "title": key,
        "scope_paths": [key],
        "doc_uids": [],
        "file_count": 10,
    }


# ── Grouping ─────────────────────────────────────────────────────────────


def test_the_default_subsystem_lenses_split_into_the_shipped_groups():
    keys = [
        "bugs",
        "security",
        "performance",
        "error-handling",
        "simplification",
        "refactor-opportunities",
        "legacy-patterns",
        "test-gaps",
    ]
    assert group_lens_keys(keys) == [
        ["bugs", "security"],
        ["performance", "error-handling"],
        ["simplification", "refactor-opportunities", "legacy-patterns", "test-gaps"],
    ]


def test_empty_groups_disappear():
    assert group_lens_keys(["bugs"]) == [["bugs"]]


def test_an_unknown_lens_gets_its_own_group_rather_than_vanishing():
    """A custom or newly-seeded lens must never be silently dropped from a
    grouped campaign — that would be a lens the user selected and never got."""
    assert group_lens_keys(["bugs", "my-custom-lens"]) == [
        ["bugs"],
        ["my-custom-lens"],
    ]


def test_every_grouped_key_is_a_real_group_member():
    flat = [k for group in LENS_GROUPS for k in group]
    assert len(flat) == len(set(flat)), "a lens appears in two groups"


# ── Planning ─────────────────────────────────────────────────────────────


def test_single_is_the_default_and_keeps_one_part_per_area():
    parts = build_plan_by_kind(
        "subsystem", [_area("a")], [_lens("bugs"), _lens("security")]
    )
    assert len(parts) == 1
    assert parts[0]["lens_keys"] == ["bugs", "security"]


def test_grouped_emits_one_part_per_area_and_lens_group():
    parts = build_plan_by_kind(
        "subsystem",
        [_area("a")],
        [_lens("bugs"), _lens("performance")],
        lens_grouping="grouped",
    )
    assert [p["lens_keys"] for p in parts] == [["bugs"], ["performance"]]
    # Same scope, different lenses — the point is attention, not partition.
    assert {tuple(p["scope_paths"]) for p in parts} == {("a",)}


def test_grouped_parts_are_titled_by_their_lenses():
    parts = build_plan_by_kind(
        "subsystem", [_area("a")], [_lens("bugs"), _lens("security")],
        lens_grouping="grouped",
    )
    assert parts[0]["title"] == "a — bugs/security"


def test_grouped_indices_stay_contiguous():
    """The tick keys every dispatch decision on idx."""
    parts = build_plan_by_kind(
        "subsystem",
        [_area("a"), _area("b")],
        [_lens("bugs"), _lens("performance")],
        lens_grouping="grouped",
        synthesis=True,
    )
    assert [p["idx"] for p in parts] == list(range(len(parts)))
    assert parts[-1]["kind"] == "synthesis"


# ── FORCE re-seed ────────────────────────────────────────────────────────


async def test_force_reseed_demands_explicit_confirmation():
    """It DISCARDS user edits, so a stray call must not be able to trigger it."""
    from fastapi import HTTPException

    from api.v1.platform_config import ReseedRequest, force_reseed_endpoint

    with pytest.raises(HTTPException) as exc:
        await force_reseed_endpoint(ReseedRequest(confirm=""), user=None)
    assert exc.value.status_code == 422
    assert "overwrites user-edited" in str(exc.value.detail)


async def test_force_reseed_runs_the_named_seeders_in_force_mode(monkeypatch):
    from api.v1.platform_config import ReseedRequest, force_reseed_endpoint
    import api.v1.platform_config as pc

    captured = {}

    async def fake_run_seeders(mode, *, names=None, **_kw):
        captured["mode"] = mode
        captured["names"] = names
        from infrastructure.seeding.base import SeedResult

        return {"lenses": SeedResult(name="lenses", updated=10)}

    async def fake_audit(**kw):
        captured["audit"] = kw

    monkeypatch.setattr(pc, "write_audit", fake_audit)
    import infrastructure.seeding as seeding

    monkeypatch.setattr(seeding, "run_seeders", fake_run_seeders)

    from types import SimpleNamespace

    out = await force_reseed_endpoint(
        ReseedRequest(confirm="FORCE", names=["lenses"]),
        user=SimpleNamespace(uid="admin1"),
    )

    from infrastructure.seeding import SeedMode

    assert captured["mode"] is SeedMode.FORCE
    assert captured["names"] == ["lenses"]
    assert out["lenses"]["updated"] == 10
    assert captured["audit"]["kind"] == "platform.force_reseed"


async def test_no_names_means_every_platform_seeder(monkeypatch):
    from api.v1.platform_config import ReseedRequest, force_reseed_endpoint
    import api.v1.platform_config as pc
    import infrastructure.seeding as seeding

    captured = {}

    async def fake_run_seeders(mode, *, names=None, **_kw):
        captured["names"] = names
        return {}

    async def fake_audit(**_kw):
        return None

    monkeypatch.setattr(pc, "write_audit", fake_audit)
    monkeypatch.setattr(seeding, "run_seeders", fake_run_seeders)

    from types import SimpleNamespace

    await force_reseed_endpoint(
        ReseedRequest(confirm="FORCE"), user=SimpleNamespace(uid="admin1")
    )
    assert captured["names"] is None
