"""propose_area_edit tells the agent what it has NOT covered yet.

The tool already returned the overlaps a proposal creates; a run could still
map a third of the tree, see zero warnings, and stop. Each proposal now also
reports what the axis it proposed into still leaves unclaimed — the in-loop
half of the partition invariant (the executor's continuation gate is the
end-of-run half).

Neo4j and the git provider are faked at the lazy-import seams.
"""

from types import SimpleNamespace

import pytest

from domains.platform_tools import areas_tools

pytestmark = pytest.mark.asyncio


class _Nodes:
    def __init__(self, rows):
        self._rows = list(rows)

    async def filter(self, **kw):
        return [
            r for r in self._rows if all(getattr(r, k, None) == v for k, v in kw.items())
        ]

    async def get_or_none(self, **kw):
        return next(
            (r for r in self._rows if all(getattr(r, k, None) == v for k, v in kw.items())),
            None,
        )


def _area(key, kind="subsystem", scope_paths=(), enabled=True):
    return SimpleNamespace(
        uid=key, repository_uid="repo1", key=key, kind=kind,
        scope_paths=list(scope_paths), enabled=enabled,
    )


@pytest.fixture(autouse=True)
def _clear_tree_cache():
    areas_tools._tree_cache.clear()
    yield
    areas_tools._tree_cache.clear()


def _wire(monkeypatch, *, tree, areas=(), edits=(), warnings=()):
    """Fake the proposal write plus the two reads coverage needs."""
    recorded = {}

    async def _propose(**kwargs):
        recorded.update(kwargs)
        return {"status": "ok", "area_edit_uid": "e1", "area_uid": "",
                "new_area": True, "warnings": list(warnings)}

    monkeypatch.setattr(areas_tools.area_service, "propose_area_edit", _propose)
    monkeypatch.setattr(
        "domains.repositories.models.Repository",
        SimpleNamespace(nodes=_Nodes([SimpleNamespace(uid="repo1")])),
    )

    async def _tree(repo):
        return list(tree), ""

    monkeypatch.setattr("domains.repositories.services.file_tree.file_tree_paths", _tree)
    monkeypatch.setattr("domains.areas.models.Area", SimpleNamespace(nodes=_Nodes(areas)))
    monkeypatch.setattr("domains.areas.models.AreaEdit", SimpleNamespace(nodes=_Nodes(edits)))
    return recorded


async def test_a_proposal_reports_what_the_axis_still_leaves_unclaimed(monkeypatch):
    _wire(monkeypatch, tree=["back_end/app.py", "front_end/main.ts", "uv.lock"],
          areas=[_area("backend", scope_paths=["back_end"])])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="backend", kind="subsystem",
        scope_paths=["back_end"], spec="the API",
    )

    gap = [w for w in result["warnings"] if "subsystem axis incomplete" in w]
    assert gap, result["warnings"]
    assert "front_end/main.ts" in gap[0] and "uv.lock" in gap[0]


async def test_the_gap_warning_joins_the_overlap_warnings(monkeypatch):
    _wire(monkeypatch, tree=["a.py", "b.py"], warnings=["scope 'a.py' overlaps leaf 'other'"])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="backend", scope_paths=["a.py"], spec="x",
    )

    assert result["warnings"][0].startswith("scope 'a.py' overlaps")
    assert "subsystem axis incomplete" in result["warnings"][1]


async def test_a_complete_axis_gets_no_gap_warning(monkeypatch):
    _wire(monkeypatch, tree=["back_end/app.py"],
          areas=[_area("backend", scope_paths=["back_end"])])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="backend", scope_paths=["back_end"], spec="x",
    )

    assert result["warnings"] == []


async def test_the_axis_reported_is_the_one_just_proposed_into(monkeypatch):
    """A feature proposal must hear about the FEATURE axis — telling it about
    subsystem gaps mid-feature-pass is noise it cannot act on."""
    _wire(monkeypatch, tree=["back_end/app.py", "front_end/main.ts"])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="features/checkout", kind="feature",
        scope_paths=["back_end/app.py"], spec="x",
    )

    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("feature axis incomplete")


async def test_feature_ignore_closes_the_feature_axis(monkeypatch):
    _wire(monkeypatch, tree=["uv.lock"],
          areas=[_area("features/none", kind="feature_ignore", scope_paths=["uv.lock"])])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="features/none", kind="feature_ignore",
        scope_paths=["uv.lock"], spec="lockfile — no feature owns it",
    )

    assert result["warnings"] == []


async def test_an_unavailable_file_tree_warns_about_nothing(monkeypatch):
    """No tree means "unknown coverage" — never "you covered nothing"."""
    _wire(monkeypatch, tree=[])

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="backend", scope_paths=["back_end"], spec="x",
    )

    assert result["warnings"] == []


async def test_a_coverage_failure_never_breaks_the_proposal(monkeypatch):
    recorded = _wire(monkeypatch, tree=["a.py"])

    async def _boom(repo):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr("domains.repositories.services.file_tree.file_tree_paths", _boom)

    result = await areas_tools.propose_area_edit(
        repository_uid="repo1", key="backend", scope_paths=["back_end"], spec="x",
    )

    assert result["status"] == "ok" and result["area_edit_uid"] == "e1"
    assert recorded["key"] == "backend"  # the edit was still recorded


async def test_the_tree_is_fetched_once_per_run_not_once_per_proposal(monkeypatch):
    """A map run proposes dozens of areas; a provider round-trip per proposal
    would make the feedback loop cost more than the mapping."""
    _wire(monkeypatch, tree=["a.py"])
    calls = {"n": 0}

    async def _counting_tree(repo):
        calls["n"] += 1
        return ["a.py"], ""

    monkeypatch.setattr(
        "domains.repositories.services.file_tree.file_tree_paths", _counting_tree
    )

    for key in ("one", "two", "three"):
        await areas_tools.propose_area_edit(
            repository_uid="repo1", key=key, scope_paths=["nope"], spec="x",
        )

    assert calls["n"] == 1
