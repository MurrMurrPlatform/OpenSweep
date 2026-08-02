"""The producer→consumer seam that `selection="stale"` rides on.

`build_plan_by_kind` filters subsystem parts on `area["stale"]`, and
`test_campaign_planner` proves that filter works — but it hand-builds area
dicts that already carry the key. Production never did: `_area_map_inputs`
set `stale` on its leaf dicts, then `areas_from_map` rebuilt every area
through `_area()`, which emits only title/scope_paths/doc_uids/file_count.
The key was dropped in between, so EVERY stale subsystem campaign planned
zero parts and finalized `done` with an empty summary — from an option the
New Campaign dialog offers as "Stale — code changed since last review".

So these tests deliberately start one layer lower than the planner tests:
real `Area` rows in, part list out, only the DB and file-tree seams stubbed.
A unit test that fabricates the seam cannot catch a seam that isn't wired.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.services import campaign_service


def _area_row(key, *, kind="subsystem", stale=False, spec="", scope=None):
    """A stand-in for a neomodel Area row — `_area_map_inputs` only reads
    attributes, and `area_is_stale` derives from the two timestamps."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return SimpleNamespace(
        key=key,
        title=key.replace("/", " ").title(),
        kind=kind,
        enabled=True,
        repository_uid="repo1",
        scope_paths=scope if scope is not None else [f"src/{key}"],
        doc_uids=[],
        spec=spec,
        # stale ⇔ code moved after the last review (freshness.is_stale).
        code_changed_at=now,
        last_reviewed_at=None if stale else now,
    )


@pytest.fixture
def plan_seams(monkeypatch):
    """Stub only the DB and the git file tree; every planner call runs real."""
    rows: list = []
    tree: list[str] = []

    class _Nodes:
        @staticmethod
        async def all():
            return list(rows)

    import domains.areas.models as area_models
    import domains.repositories.models as repo_models

    monkeypatch.setattr(area_models.Area, "nodes", _Nodes)

    class _RepoNodes:
        @staticmethod
        async def get_or_none(**_kw):
            return SimpleNamespace(uid="repo1")

    monkeypatch.setattr(repo_models.Repository, "nodes", _RepoNodes)

    async def fake_file_tree(_repo):
        return list(tree), ""

    async def fake_enabled_lenses(_kind, _lens_keys):
        return [{"key": "bugs", "global_agent_key": "", "enabled": True}]

    monkeypatch.setattr(campaign_service, "file_tree_paths", fake_file_tree)
    monkeypatch.setattr(campaign_service, "_enabled_lenses", fake_enabled_lenses)
    return SimpleNamespace(rows=rows, tree=tree)


def _files(key, count=60):
    """Enough files that the leaf stands alone — `bundle_siblings` merges
    same-parent leaves under DEFAULT_TARGET_MIN (50), which would otherwise
    collapse these fixtures into one part and hide what they assert."""
    return [f"src/{key}/f{i}.py" for i in range(count)]


async def _plan(kind="subsystem", selection="stale"):
    parts, _degraded, _source, _summary = await campaign_service._plan_parts(
        "repo1",
        kind=kind,
        coverage_keys=[],
        selection=selection,
        lens_keys=["bugs"],
        k=3,
    )
    # The synthesis part is appended to every non-empty plan and is not what
    # these tests are about; it has its own coverage in test_campaign_synthesis.
    return [p for p in parts if p["kind"] != "synthesis"]


@pytest.mark.asyncio
async def test_stale_subsystem_campaign_plans_the_stale_areas(plan_seams):
    plan_seams.rows.extend(
        [
            _area_row("backend", stale=True),
            _area_row("frontend", stale=False),
        ]
    )
    plan_seams.tree.extend(_files("backend") + _files("frontend"))

    parts = await _plan()

    # The regression: this list was empty for every repo, always.
    assert parts, "stale selection planned zero parts — the `stale` key was dropped"
    assert {p["title"] for p in parts} == {"Backend"}


@pytest.mark.asyncio
async def test_stale_selection_still_excludes_fresh_areas(plan_seams):
    plan_seams.rows.append(_area_row("frontend", stale=False))
    plan_seams.tree.extend(_files("frontend"))

    assert await _plan() == []


@pytest.mark.asyncio
async def test_selection_all_is_unaffected_by_staleness(plan_seams):
    plan_seams.rows.extend(
        [_area_row("backend", stale=True), _area_row("frontend", stale=False)]
    )
    plan_seams.tree.extend(_files("backend") + _files("frontend"))

    parts = await _plan(selection="all")

    assert {p["title"] for p in parts} == {"Backend", "Frontend"}


@pytest.mark.asyncio
async def test_unaudited_selects_areas_whose_code_moved_since_the_last_audit(
    plan_seams, monkeypatch
):
    """`stale` and `unaudited` are different questions. An area can be
    perfectly mapped (never stale) and years out of audit — before this
    selection existed there was no way to ask for it."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    # Both areas are map-fresh; only `backend` was audited before its last
    # code change.
    plan_seams.rows.extend(
        [_area_row("backend", stale=False), _area_row("frontend", stale=False)]
    )
    plan_seams.tree.extend(_files("backend") + _files("frontend"))

    async def fake_recency(_repo):
        return {
            "src/backend": now - timedelta(days=90),  # audited long before
            "src/frontend": now + timedelta(days=1),  # audited after
        }

    # Lazily imported inside _plan_parts, so patch it at the source module.
    import domains.runs.services.audit_selection as audit_selection

    monkeypatch.setattr(audit_selection, "coverage_recency_for", fake_recency)

    parts = await _plan(selection="unaudited")

    assert {p["title"] for p in parts} == {"Backend"}


@pytest.mark.asyncio
async def test_unaudited_includes_areas_never_audited_at_all(plan_seams, monkeypatch):
    plan_seams.rows.append(_area_row("backend", stale=False))
    plan_seams.tree.extend(_files("backend"))

    async def fake_recency(_repo):
        return {}  # no coverage stamps anywhere

    # Lazily imported inside _plan_parts, so patch it at the source module.
    import domains.runs.services.audit_selection as audit_selection

    monkeypatch.setattr(audit_selection, "coverage_recency_for", fake_recency)

    assert [p["title"] for p in await _plan(selection="unaudited")] == ["Backend"]


@pytest.mark.asyncio
async def test_a_bundle_is_stale_when_any_bundled_leaf_is(plan_seams):
    """Undersized siblings share one run, so the part needs a look as soon as
    any leaf in it does — otherwise bundling would hide staleness."""
    plan_seams.rows.extend(
        [
            _area_row("backend/api", stale=False, scope=["src/api"]),
            _area_row("backend/jobs", stale=True, scope=["src/jobs"]),
        ]
    )
    plan_seams.tree.extend(["src/api/a.py", "src/jobs/b.py"])

    parts = await _plan()

    assert len(parts) == 1
    assert set(parts[0]["area_keys"]) == {"backend/api", "backend/jobs"}
