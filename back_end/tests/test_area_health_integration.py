"""P0 — the merged Areas board, against the real test Neo4j (localhost:7999).

Two things the separate Health page could not do, so they are what this pins:
a grouping row reporting its children's staleness (a grouping owns no
scope_paths and can never itself be stale), and audit coverage resolved per
AREA via covered-path overlap rather than per Doc.

Also covers the freshness reconcile cursor: the backstop that repairs a
dropped push webhook, which previously had nothing to notice it.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from domains.areas.models import Area
from domains.areas.services.area_health import area_health
from domains.checked.models import Checked
from domains.docs.models import Doc
from domains.repositories.models import Repository

pytestmark = pytest.mark.integration


def _repo_uid() -> str:
    return "repo-" + uuid4().hex[:8]


async def _make_repo(uid: str, **kw) -> Repository:
    r = Repository(
        uid=uid,
        org_uid="org-" + uuid4().hex[:6],
        slug="s" + uuid4().hex[:6],
        mode="github",
        name="repo",
        default_branch="main",
        **kw,
    )
    await r.save()
    return r


async def _make_area(repo: str, key: str, scopes: list[str], **kw) -> Area:
    a = Area(
        uid=uuid4().hex,
        repository_uid=repo,
        key=key,
        kind=kw.pop("kind", "subsystem"),
        scope_paths=list(scopes),
        spec=kw.pop("spec", "what to check"),
        last_reviewed_at=kw.pop("last_reviewed_at", datetime.now(UTC)),
        **kw,
    )
    await a.save()
    return a


async def _make_doc(repo: str, slug: str, watch: list[str], **kw) -> Doc:
    d = Doc(
        uid=uuid4().hex,
        repository_uid=repo,
        slug=slug,
        watch_paths=list(watch),
        last_reviewed_at=kw.pop("last_reviewed_at", datetime.now(UTC)),
        **kw,
    )
    await d.save()
    return d


async def _make_stamp(repo: str, covered: list[str], **kw) -> Checked:
    c = Checked(
        uid=uuid4().hex,
        repository_uid=repo,
        scope_uid=kw.pop("scope_uid", repo),
        run_uid=uuid4().hex,
        covered_paths=list(covered),
        outcome=kw.pop("outcome", "clean"),
        checked_at=kw.pop("checked_at", datetime.now(UTC)),
        **kw,
    )
    await c.save()
    return c


def _row(result, key):
    return next(r for r in result.rows if r.key == key)


async def test_grouping_row_reports_stale_descendants(monkeypatch):
    """The bug the merge fixes: a parent read "clean" over stale leaves."""
    repo = _repo_uid()
    await _make_repo(repo)
    now = datetime.now(UTC)
    await _make_area(repo, "backend", ["back_end"])
    await _make_area(
        repo,
        "backend/api",
        ["back_end/api"],
        last_reviewed_at=now - timedelta(days=2),
        code_changed_at=now,  # stale
    )
    await _make_area(repo, "backend/jobs", ["back_end/jobs"])

    result = await area_health(repo)

    parent = _row(result, "backend")
    assert parent.is_leaf is False
    assert parent.stale_descendants == 1
    assert _row(result, "backend/api").stale is True
    assert _row(result, "backend/jobs").stale_descendants == 0


async def test_audit_coverage_resolves_per_area_by_covered_paths():
    """Coverage is matched to areas through the paths a run examined — the
    Health page could only key stamps by Doc."""
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "api", ["src/api"])
    await _make_area(repo, "web", ["src/web"])
    await _make_stamp(repo, ["src/api/handlers.py"], outcome="findings")

    result = await area_health(repo)

    api = _row(result, "api")
    assert api.outcome == "findings"
    assert api.last_checked is not None
    web = _row(result, "web")
    assert web.last_checked is None  # never audited, not silently "clean"


async def test_latest_stamp_wins_per_area():
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "api", ["src/api"])
    now = datetime.now(UTC)
    await _make_stamp(repo, ["src/api/a.py"], outcome="findings", checked_at=now - timedelta(days=3))
    await _make_stamp(repo, ["src/api/a.py"], outcome="clean", checked_at=now)

    assert _row(await area_health(repo), "api").outcome == "clean"


async def test_docs_roll_into_the_covering_area():
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "api", ["src/api"])
    now = datetime.now(UTC)
    await _make_doc(repo, "api-guide", ["src/api"])
    await _make_doc(
        repo,
        "api-internals",
        ["src/api/internals"],
        last_reviewed_at=now - timedelta(days=2),
        code_changed_at=now,  # stale page
    )

    api = _row(await area_health(repo), "api")
    assert api.docs_total == 2
    assert api.docs_stale == 1


async def test_docs_outside_every_area_are_surfaced_not_dropped():
    """Folding Health into Areas must not silently lose pages the map doesn't
    cover — that would hide a real gap."""
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "api", ["src/api"])
    await _make_doc(repo, "orphan", ["docs/handbook"])

    result = await area_health(repo)
    assert [d.slug for d in result.unassigned_docs] == ["orphan"]


async def test_archived_docs_are_excluded():
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "api", ["src/api"])
    await _make_doc(repo, "retired", ["src/api"], archived=True)

    result = await area_health(repo)
    assert _row(result, "api").docs_total == 0
    assert result.unassigned_docs == []


async def test_summary_counts_only_auditable_leaves():
    """Groupings own no files and ignore areas are explicitly not audit
    targets — counting either would inflate every tile."""
    repo = _repo_uid()
    await _make_repo(repo)
    now = datetime.now(UTC)
    await _make_area(repo, "backend", ["back_end"])  # grouping
    await _make_area(
        repo, "backend/api", ["back_end/api"],
        last_reviewed_at=now - timedelta(days=1), code_changed_at=now,
    )
    await _make_area(repo, "backend/jobs", ["back_end/jobs"])
    await _make_area(repo, "vendor", ["vendor"], kind="ignore", spec="generated")

    s = (await area_health(repo)).summary
    assert s.total == 2  # the two subsystem leaves only
    assert s.stale == 1
    # backend/api is stale AND unaudited; it belongs to exactly one tile.
    assert s.never_audited == 1
    assert s.fresh == 0
    assert s.untracked == 0
    assert s.untracked + s.stale + s.never_audited + s.fresh == s.total


async def test_summary_tiles_partition_every_auditable_leaf():
    """The four state tiles must sum to total — they used to double-count a
    leaf that was both stale and never audited, and drop one that was
    neither."""
    repo = _repo_uid()
    await _make_repo(repo)
    now = datetime.now(UTC)
    # stale + never audited (the cell that used to be counted twice)
    await _make_area(
        repo, "a", ["src/a"],
        last_reviewed_at=now - timedelta(days=1), code_changed_at=now,
    )
    # tracked, fresh, never audited (the cell that used to be counted in none)
    await _make_area(repo, "b", ["src/b"])
    # untracked leaf — no scope_paths at all
    await _make_area(repo, "c", [])

    s = (await area_health(repo)).summary
    assert s.total == 3
    assert s.stale == 1
    assert s.never_audited == 1
    assert s.untracked == 1
    assert s.fresh == 0
    assert s.untracked + s.stale + s.never_audited + s.fresh == s.total


async def test_unscoped_leaf_is_untracked_not_fresh():
    """A leaf with no scope_paths can never be marked stale and can never
    receive coverage — the webhook has nothing to match it against. Counting
    it as fresh put the map's most broken rows in its healthiest tile."""
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_area(repo, "orphan", [])

    health = await area_health(repo)
    row = _row(health, "orphan")
    assert row.tracked is False
    assert row.stale is False  # nothing can mark it — that is the whole point
    assert health.summary.untracked == 1
    assert health.summary.fresh == 0


async def test_rollup_is_scoped_to_one_repository():
    other = _repo_uid()
    repo = _repo_uid()
    await _make_repo(repo)
    await _make_repo(other)
    await _make_area(repo, "mine", ["src"])
    await _make_area(other, "theirs", ["src"])
    await _make_doc(other, "theirs-doc", ["src"])

    result = await area_health(repo)
    assert [r.key for r in result.rows] == ["mine"]
    assert result.unassigned_docs == []


async def test_freshness_cursor_is_surfaced_for_the_as_of_line():
    repo = _repo_uid()
    await _make_repo(
        repo,
        freshness_synced_sha="a" * 40,
        freshness_synced_at=datetime.now(UTC),
        freshness_degraded_reason="compare truncated at 300 files",
    )
    await _make_area(repo, "api", ["src/api"])

    result = await area_health(repo)
    assert result.freshness_degraded_reason == "compare truncated at 300 files"
    assert result.freshness_synced_at is not None
