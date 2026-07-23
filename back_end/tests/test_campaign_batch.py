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
    reads back, write_audit is a no-op, record_event captures on the row."""
    rows: dict[str, Campaign] = {}

    async def fake_save(self):
        rows[self.uid] = self
        return self

    class _Nodes:
        @staticmethod
        async def get_or_none(uid=None, **_kw):
            return rows.get(uid)

    monkeypatch.setattr(Campaign, "save", fake_save)
    monkeypatch.setattr(Campaign, "nodes", _Nodes)

    async def fake_audit(**_kw):
        return None

    monkeypatch.setattr(batch, "write_audit", fake_audit)

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


async def test_launch_batch_launches_each_child_and_runs_the_parent(
    store, create_seam, monkeypatch
):
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))
    launched = []

    async def fake_launch(uid, **_kw):
        launched.append(uid)
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(campaign_service, "launch", fake_launch)

    await batch.launch_batch(parent)

    assert sorted(launched) == sorted(parent.child_uids)
    assert store.rows[parent.uid].status == "running"


async def test_batch_parent_finalizes_when_one_child_fails_to_launch(
    store, create_seam, monkeypatch
):
    """Regression: a child whose launch() raises must be cancelled (not left in
    planning) so aggregate_batch can see all children as terminal and the parent
    can reach done rather than hanging in running forever."""
    parent = await batch.create_batch("repo1", CreateCampaignRequest(kind="batch"))

    # Pick the first child uid — its launch will raise; the others succeed.
    failing_uid = parent.child_uids[0]

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

    # aggregate_batch must now return True (parent reaches done).
    fresh_parent = store.rows[parent.uid]
    result = await batch.aggregate_batch(fresh_parent)
    assert result is True, "aggregate_batch should have finalized the parent"
    assert store.rows[parent.uid].status == "done", (
        "parent must reach done — it was hanging in running (the original bug)"
    )
