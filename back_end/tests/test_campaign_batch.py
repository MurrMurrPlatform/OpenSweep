"""Batch campaigns — fan-out + roll-up with the DB/service seams stubbed.

create_batch makes a parent (kind="batch", no parts) plus three children
(subsystem/feature/global) each carrying parent_uid; aggregate_batch is a
no-op while any child is live and finalizes the parent (summing child
summary.counts.total) once every child is terminal.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.schemas import CreateCampaignRequest
from domains.campaigns.services import batch, campaign_service


@pytest.fixture
def store(monkeypatch):
    """In-memory Campaign store: save() upserts by uid, nodes.get_or_none
    reads back, write_audit is a no-op, record_event captures on the row,
    and the atomic-transition Cypher applies the same predecessor gate the
    real query would (used by any campaign_service.cancel that runs through
    the real implementation, e.g. _launch_child's cancel-on-failure path)."""
    rows: dict[str, Campaign] = {}

    async def fake_save(self):
        rows[self.uid] = self
        return self

    class _Nodes:
        @staticmethod
        async def get_or_none(uid=None, **_kw):
            return rows.get(uid)

    async def fake_cypher(query, params=None):
        params = params or {}
        row = rows.get(params.get("uid"))
        if row is None:
            return [], None
        current = row.status or "planning"
        if current not in set(params.get("predecessors") or []):
            return [], None
        row.status = params["to"]
        return [[row.status]], None

    from neomodel import adb as _adb

    monkeypatch.setattr(_adb, "cypher_query", fake_cypher)
    monkeypatch.setattr(Campaign, "save", fake_save)
    monkeypatch.setattr(Campaign, "nodes", _Nodes)

    async def fake_audit(**_kw):
        return None

    monkeypatch.setattr(batch, "write_audit", fake_audit)
    monkeypatch.setattr(campaign_service, "write_audit", fake_audit)

    async def fake_record_event(c, type, **payload):
        c.events = [*(c.events or []), {"type": type, **payload}]

    monkeypatch.setattr(campaign_service, "record_event", fake_record_event)
    return SimpleNamespace(rows=rows)


@pytest.fixture
def create_seam(monkeypatch):
    """campaign_service.create → an in-memory child Campaign per kind."""
    created = []

    async def fake_create(repository_uid, req, *, created_by="", trigger_provenance=""):
        child = Campaign(
            uid=f"child-{req.kind}",
            repository_uid=repository_uid,
            title=req.title or "",
            status="planning",
            kind=req.kind,
            selection=req.selection or "all",
            coverage_keys=list(req.coverage_keys or []),
            effort=req.effort or "",
            parts=[],
        )
        await child.save()
        created.append(child)
        return child

    monkeypatch.setattr(campaign_service, "create", fake_create)
    return created


async def test_create_batch_makes_three_children_with_distinct_kinds(
    store, create_seam
):
    req = CreateCampaignRequest(kind="batch", effort="deep", selection="stale")
    parent = await batch.create_batch(
        "repo1", req, created_by="u1", trigger_provenance="manual"
    )

    assert parent.kind == "batch"
    assert parent.parts == []  # a batch parent owns no parts
    assert len(parent.child_uids) == 3

    children = [store.rows[uid] for uid in parent.child_uids]
    assert {c.kind for c in children} == {"subsystem", "feature", "global"}
    # Every child points back at the parent and shares effort/selection.
    assert all(c.parent_uid == parent.uid for c in children)
    assert all(c.effort == "deep" for c in children)
    assert all(c.selection == "stale" for c in children)


async def test_aggregate_batch_noop_while_a_child_runs(store, create_seam):
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    parent.status = "running"
    await parent.save()
    # Two children terminal, one still running.
    kids = [store.rows[uid] for uid in parent.child_uids]
    kids[0].status = "done"
    kids[1].status = "failed"
    kids[2].status = "running"
    for k in kids:
        await k.save()

    assert await batch.aggregate_batch(parent) is False
    assert store.rows[parent.uid].status == "running"  # parent unchanged


async def test_aggregate_batch_finalizes_and_sums_child_totals(store, create_seam):
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    parent.status = "running"
    await parent.save()

    kids = [store.rows[uid] for uid in parent.child_uids]
    totals = [3, 5, 0]
    statuses = ["done", "done", "failed"]
    for k, total, status in zip(kids, totals, statuses, strict=True):
        k.status = status
        k.summary = {"counts": {"total": total}}
        await k.save()

    assert await batch.aggregate_batch(parent) is True
    fresh = store.rows[parent.uid]
    assert fresh.status == "done"
    assert fresh.summary["totals"]["total"] == 8  # 3 + 5 + 0
    child_rows = fresh.summary["children"]
    assert len(child_rows) == 3
    assert {r["kind"] for r in child_rows} == {"subsystem", "feature", "global"}
    assert sorted(r["counts"]["total"] for r in child_rows) == [0, 3, 5]


@pytest.fixture
def launch_seam(monkeypatch):
    """campaign_service.launch → records the uid and flips the row running."""
    launched: list[str] = []

    async def fake_launch(uid, **_kw):
        launched.append(uid)
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(campaign_service, "launch", fake_launch)
    return launched


def _child_of_kind(store, parent, kind):
    return next(store.rows[u] for u in parent.child_uids if store.rows[u].kind == kind)


async def test_launch_batch_launches_the_siblings_and_defers_the_global_child(
    store, create_seam, launch_seam
):
    """The global child is held back — see the batch module docstring.

    Inside one campaign `plan_tick` holds global parts until the area parts
    are terminal. A batch puts the globals in their OWN campaign, where that
    check is `all([])` — vacuously true — so launching all three at once
    dispatched the whole-repo sweeps with an empty escalation digest.
    """
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))

    await batch.launch_batch(parent)

    global_child = _child_of_kind(store, parent, "global")
    assert global_child.uid not in launch_seam
    assert global_child.status == "planning"
    assert sorted(launch_seam) == sorted(
        u for u in parent.child_uids if u != global_child.uid
    )
    assert store.rows[parent.uid].status == "running"


async def test_the_parent_cannot_finalize_while_the_global_child_waits(
    store, create_seam, launch_seam
):
    """A deferred child is not terminal, so the roll-up must stay open."""
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    await batch.launch_batch(parent)

    for kind in ("subsystem", "feature"):
        child = _child_of_kind(store, parent, kind)
        child.status = "done"
        await child.save()

    assert await batch.aggregate_batch(store.rows[parent.uid]) is False


async def test_advance_batch_waits_while_a_sibling_is_still_running(
    store, create_seam, launch_seam
):
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    await batch.launch_batch(parent)
    launch_seam.clear()

    _child_of_kind(store, parent, "subsystem").status = "done"
    _child_of_kind(store, parent, "feature").status = "running"

    assert await batch.advance_batch(store.rows[parent.uid]) == 0
    assert launch_seam == []
    assert _child_of_kind(store, parent, "global").status == "planning"


async def test_advance_batch_releases_the_global_child_once_siblings_finish(
    store, create_seam, launch_seam
):
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    await batch.launch_batch(parent)
    launch_seam.clear()

    # One done, one failed — a failed sibling still counts as terminal, exactly
    # as plan_tick treats a failed area part as unlocking the globals.
    _child_of_kind(store, parent, "subsystem").status = "done"
    _child_of_kind(store, parent, "feature").status = "failed"

    global_uid = _child_of_kind(store, parent, "global").uid
    assert await batch.advance_batch(store.rows[parent.uid]) == 1
    assert launch_seam == [global_uid]

    # ...and it only fires once: the child is no longer in `planning`.
    _child_of_kind(store, parent, "global").status = "running"
    launch_seam.clear()
    assert await batch.advance_batch(store.rows[parent.uid]) == 0
    assert launch_seam == []


async def test_a_deferred_child_that_fails_to_launch_is_cancelled(
    store, create_seam, monkeypatch
):
    """Otherwise the parent hangs in running forever — same failure mode the
    immediate-launch path already guards against."""
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    global_uid = _child_of_kind(store, parent, "global").uid

    async def fake_launch(uid, **_kw):
        if uid == global_uid:
            raise RuntimeError("simulated dispatch error")
        return SimpleNamespace(uid=uid)

    async def fake_cancel(uid, *, reason="", actor_uid=""):
        store.rows[uid].status = "cancelled"

    monkeypatch.setattr(campaign_service, "launch", fake_launch)
    monkeypatch.setattr(campaign_service, "cancel", fake_cancel)

    await batch.launch_batch(parent)
    for kind in ("subsystem", "feature"):
        child = _child_of_kind(store, parent, kind)
        child.status = "done"
        child.summary = {"counts": {"total": 1}}

    assert await batch.advance_batch(store.rows[parent.uid]) == 0
    assert _child_of_kind(store, parent, "global").status == "cancelled"
    assert await batch.aggregate_batch(store.rows[parent.uid]) is True
    assert store.rows[parent.uid].status == "done"


async def test_batch_parent_finalizes_when_one_child_fails_to_launch(
    store, create_seam, monkeypatch
):
    """Regression: a child whose launch() raises must be cancelled (not left in
    planning) so aggregate_batch can see all children as terminal and the parent
    can reach done rather than hanging in running forever."""
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))

    # The subsystem child launches immediately; its launch will raise. (The
    # global child is deferred, so it cannot stand in for this case — see
    # test_a_deferred_child_that_fails_to_launch_is_cancelled.)
    failing_uid = _child_of_kind(store, parent, "subsystem").uid

    async def fake_launch(uid, **_kw):
        if uid == failing_uid:
            raise RuntimeError("simulated dispatch error")
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(campaign_service, "launch", fake_launch)

    await batch.launch_batch(parent)

    # Parent must be running after launch_batch.
    assert store.rows[parent.uid].status == "running"

    # The failed child must be in a terminal state (cancelled), not stuck in
    # planning — that was the bug.
    failed_child = store.rows[failing_uid]
    assert failed_child.status in {"cancelled", "failed", "done"}, (
        f"expected terminal status for failed child, got {failed_child.status!r}"
    )

    # Drive the surviving children to done so aggregate_batch can finalize.
    for uid in parent.child_uids:
        if uid != failing_uid:
            child = store.rows[uid]
            child.status = "done"
            child.summary = {"counts": {"total": 2}}
            await child.save()

    # The deferred global child is `done` above, so nothing is left to release.
    assert await batch.advance_batch(store.rows[parent.uid]) == 0

    # aggregate_batch must now return True (parent reaches done).
    fresh_parent = store.rows[parent.uid]
    result = await batch.aggregate_batch(fresh_parent)
    assert result is True, "aggregate_batch should have finalized the parent"
    assert store.rows[parent.uid].status == "done", (
        "parent must reach done — it was hanging in running (the original bug)"
    )
