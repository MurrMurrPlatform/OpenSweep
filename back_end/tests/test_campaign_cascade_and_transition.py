"""Cascade + concurrency + coverage-scoping guarantees added in the
`Campaigns domain: cascade, concurrency & coverage-scoping` ticket:

- `_transition` is a single atomic Cypher CAS (write only if current status
  is in the legal predecessor set) — a stale in-memory node cannot force a
  transition that the store no longer allows.
- `_replan` refetches immediately before saving and bails out with a
  `replan_skipped` event when a concurrent cancel moved the campaign out of
  `planning` during the (network-bound) plan recompute.
- `cancel` on a batch parent cascades to every non-terminal child.
- `update` on a batch parent cascades a `max_parallel` retune to every
  non-terminal child (the parent's own value is never read by dispatch).
- `delete` on a batch parent 409s while any child is still live.
- `_doc_inputs` / `_area_map_inputs` push the tenant filter into the query
  instead of loading platform-wide rows and filtering in Python.
- `_dispatch_global`'s scope-note gate now fires on `coverage_keys` too, so
  a coverage-scoped global campaign actually reads the `scope_hint` the
  planner wrote for it.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.campaigns.models import Campaign
from domains.campaigns.services import campaign_service


# ── shared in-memory Campaign store + service stubs ─────────────────────────


@pytest.fixture
def store(monkeypatch):
    """Campaign.save/nodes.get_or_none/delete backed by an in-memory dict;
    the atomic-transition Cypher applies the same predecessor gate the real
    query would, against the store's current row."""
    rows: dict[str, Campaign] = {}
    audits: list[dict] = []
    events: dict[str, list[dict]] = {}
    cypher_calls: list[dict] = []

    async def fake_save(self):
        rows[self.uid] = self
        return self

    async def fake_delete(self):
        rows.pop(self.uid, None)

    class _Nodes:
        @staticmethod
        async def get_or_none(uid=None, **_kw):
            return rows.get(uid)

    async def fake_audit(**kw):
        audits.append(kw)

    async def fake_record_event(c, type, **payload):
        events.setdefault(c.uid, []).append({"type": type, **payload})

    async def fake_cypher(query, params=None):
        params = params or {}
        cypher_calls.append({"query": query, "params": params})
        # Emulate the CAS: only rewrite the row when its current status is
        # in the predecessor set the caller supplied.
        uid = params.get("uid")
        row = rows.get(uid)
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
    monkeypatch.setattr(Campaign, "delete", fake_delete)
    monkeypatch.setattr(Campaign, "nodes", _Nodes)
    monkeypatch.setattr(campaign_service, "write_audit", fake_audit)
    monkeypatch.setattr(campaign_service, "record_event", fake_record_event)
    return SimpleNamespace(
        rows=rows,
        audits=audits,
        events=events,
        cypher_calls=cypher_calls,
    )


def _campaign(**overrides) -> Campaign:
    fields = dict(
        uid="c-parent",
        repository_uid="repo1",
        title="t",
        status="planning",
        kind="subsystem",
        selection="all",
        coverage_keys=[],
        template="rotation",
        effort="",
        lens_keys=["bugs"],
        k=3,
        parts=[],
        max_parallel=2,
        child_uids=[],
    )
    fields.update(overrides)
    return Campaign(**fields)


# ── _transition: atomic CAS ─────────────────────────────────────────────────


async def test_transition_writes_via_cas_when_predecessor_matches(store):
    c = _campaign(status="planning")
    await c.save()

    await campaign_service._transition(c, "running")

    assert store.rows[c.uid].status == "running"
    # And the CAS parameters name the ONLY legal predecessor for running.
    (call,) = [
        call for call in store.cypher_calls if call["params"].get("to") == "running"
    ]
    assert call["params"]["predecessors"] == ["planning"]


async def test_transition_409s_when_store_moved_since_local_read(store):
    """A stale in-memory node showing `planning` cannot force `running` when
    the store already flipped to `cancelled` — the CAS refuses the write."""
    c = _campaign(status="planning")
    await c.save()
    # Simulate a concurrent cancel that lands between the read and this
    # transition attempt.
    store.rows[c.uid].status = "cancelled"

    with pytest.raises(HTTPException) as exc:
        await campaign_service._transition(c, "running")
    assert exc.value.status_code == 409
    assert "cancelled" in str(exc.value.detail)
    # Store stays at the concurrent write's value — no clobber.
    assert store.rows[c.uid].status == "cancelled"


async def test_predecessors_reverse_lookup_covers_every_target():
    """The reverse map is derived, not hand-maintained — check every legal
    (from, to) pair round-trips through `_predecessors_of`."""
    from domains.campaigns.models import LEGAL_STATUS_TRANSITIONS

    for frm, tos in LEGAL_STATUS_TRANSITIONS.items():
        for to in tos:
            assert frm in campaign_service._predecessors_of(to), (frm, to)


# ── _replan: refetch-before-save, drop the write if status raced away ──────


async def test_replan_bails_when_concurrent_cancel_moved_status_out_of_planning(
    store, monkeypatch
):
    """`_plan_parts` is awaited — a cancel that lands during that await must
    not have its `cancelled` status clobbered by the replan write."""
    c = _campaign(status="planning", parts=[{"idx": 0}])
    await c.save()

    async def fake_plan_parts(*_a, **_kw):
        # Simulate a concurrent cancel landing between the plan being
        # computed and it being persisted.
        store.rows[c.uid].status = "cancelled"
        return ([{"idx": 0}, {"idx": 1}], "", "docs", {"total_runs": 2})

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)

    await campaign_service._replan(c)

    # Cancel wasn't clobbered; the parts were NOT replaced.
    assert store.rows[c.uid].status == "cancelled"
    assert store.rows[c.uid].parts == [{"idx": 0}]
    (event,) = store.events[c.uid]
    assert event["type"] == "replan_skipped"
    assert "cancelled" in event["reason"]


async def test_replan_writes_when_status_stays_planning(store, monkeypatch):
    """Sanity: the happy path still writes the fresh plan."""
    c = _campaign(status="planning", parts=[{"idx": 0}])
    await c.save()

    async def fake_plan_parts(*_a, **_kw):
        return ([{"idx": 0}, {"idx": 1}], "", "docs", {"total_runs": 2})

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)

    await campaign_service._replan(c)

    assert store.rows[c.uid].parts == [{"idx": 0}, {"idx": 1}]
    (event,) = store.events[c.uid]
    assert event["type"] == "replanned"
    assert event["parts"] == 2 and event["was"] == 1


# ── batch cancel/update cascade + delete guard ─────────────────────────────


def _batch(store, **overrides) -> Campaign:
    """Sync helper to seed a parent + 3 children in the store."""
    parent = _campaign(uid="p", kind="batch", child_uids=["c-sub", "c-feat", "c-glob"])
    for k in overrides:
        setattr(parent, k, overrides[k])
    store.rows[parent.uid] = parent
    for uid, kind in (("c-sub", "subsystem"), ("c-feat", "feature"), ("c-glob", "global")):
        store.rows[uid] = _campaign(uid=uid, kind=kind, status="running")
    return parent


async def test_cancel_batch_parent_cascades_to_non_terminal_children(store):
    parent = _batch(store, status="running")

    await campaign_service.cancel(parent.uid, actor_uid="u1", reason="stop")

    # All three children moved to cancelled, then the parent.
    assert store.rows[parent.uid].status == "cancelled"
    for child_uid in parent.child_uids:
        assert store.rows[child_uid].status == "cancelled"


async def test_cancel_batch_parent_skips_already_terminal_children(store):
    parent = _batch(store, status="running")
    store.rows["c-sub"].status = "done"  # already terminal
    store.rows["c-feat"].status = "failed"

    await campaign_service.cancel(parent.uid, actor_uid="u1")

    assert store.rows["c-sub"].status == "done"  # untouched
    assert store.rows["c-feat"].status == "failed"
    assert store.rows["c-glob"].status == "cancelled"  # only the live one
    assert store.rows[parent.uid].status == "cancelled"


async def test_update_batch_cascades_max_parallel_to_children(store):
    parent = _batch(store, status="running", max_parallel=2)
    for child_uid in parent.child_uids:
        store.rows[child_uid].max_parallel = 2

    req = SimpleNamespace(max_parallel=7, title=None)
    await campaign_service.update(parent.uid, req, actor_uid="u1")

    assert store.rows[parent.uid].max_parallel == 7
    for child_uid in parent.child_uids:
        assert store.rows[child_uid].max_parallel == 7


async def test_update_batch_max_parallel_skips_terminal_children(store):
    parent = _batch(store, status="running", max_parallel=2)
    store.rows["c-sub"].status = "done"
    store.rows["c-sub"].max_parallel = 2

    req = SimpleNamespace(max_parallel=9, title=None)
    await campaign_service.update(parent.uid, req, actor_uid="u1")

    assert store.rows["c-sub"].max_parallel == 2  # terminal, not cascaded
    assert store.rows["c-feat"].max_parallel == 9
    assert store.rows["c-glob"].max_parallel == 9


async def test_delete_batch_409s_while_any_child_is_live(store):
    parent = _batch(store, status="cancelled")
    # Parent cancelled but a child is still finishing up.
    for child_uid in parent.child_uids:
        store.rows[child_uid].status = "done"
    store.rows["c-glob"].status = "finalizing"

    with pytest.raises(HTTPException) as exc:
        await campaign_service.delete(parent.uid, actor_uid="u1")
    assert exc.value.status_code == 409
    assert "c-glob" in str(exc.value.detail)
    assert parent.uid in store.rows  # still there


async def test_delete_batch_succeeds_when_every_child_is_terminal(store):
    parent = _batch(store, status="cancelled")
    for child_uid in parent.child_uids:
        store.rows[child_uid].status = "cancelled"

    await campaign_service.delete(parent.uid, actor_uid="u1")

    assert parent.uid not in store.rows


# ── coverage-scoping: _dispatch_global reads coverage_keys, not just prefix ─


async def test_dispatch_global_uses_scope_hint_when_only_coverage_keys_set(
    monkeypatch,
):
    """A global campaign scoped via `coverage_keys` (not the legacy
    `area_prefix`) still has to steer the sweep with its scope_hint —
    the gate now checks coverage_keys."""
    from domains.campaigns.services import part_dispatch

    captured = {}

    class FakeLens:
        key = "security"
        global_agent_key = "security-sweeper"

    async def fake_get_by_key(_k):
        return FakeLens()

    async def fake_variant_by_url(_u):
        return SimpleNamespace(key="security-sweeper")

    def fake_variant_source_url(_k):
        return "url://security-sweeper"

    async def fake_digest(*_a, **_kw):
        return [], []

    async def fake_mark(*_a, **_kw):
        return None

    async def fake_dispatch(**kw):
        captured.update(kw)
        return SimpleNamespace(uid="run-x")

    monkeypatch.setattr(part_dispatch.lens_service, "get_by_key", fake_get_by_key)
    monkeypatch.setattr(part_dispatch, "system_agent_by_url", fake_variant_by_url)
    monkeypatch.setattr(part_dispatch, "variant_source_url", fake_variant_source_url)
    monkeypatch.setattr(part_dispatch, "_escalation_digest", fake_digest)
    monkeypatch.setattr(part_dispatch, "_mark_escalations_delivered", fake_mark)
    monkeypatch.setattr(part_dispatch, "dispatch_agent", fake_dispatch)

    campaign = SimpleNamespace(
        uid="c1",
        repository_uid="repo1",
        title="Coverage-scoped global sweep",
        parts=[{"idx": 0}],
        area_prefix="",  # legacy field EMPTY
        coverage_keys=["backend/delivery"],
        effort="",
        trigger_provenance="manual",
        created_by="u1",
    )
    part = {
        "idx": 0,
        "kind": "global",
        "title": "Sec sweep",
        "lens_keys": ["security"],
        "scope_hint": ["back_end/domains/delivery", "back_end/api/v1/delivery.py"],
        "scope_paths": [],
        "doc_uids": [],
        "area_keys": [],
    }

    run_uid = await part_dispatch._dispatch_global(campaign, part)
    assert run_uid == "run-x"

    structural = captured["structural_extra"]
    # The scope-note fired: hint paths are surfaced, and the label falls
    # back to the coverage_keys when there's no legacy prefix.
    assert "back_end/domains/delivery" in structural
    assert "backend/delivery" in structural
    assert "Concentrate on:" in structural


# ── tenant-scoped queries (Doc / Area) ─────────────────────────────────────


async def test_doc_inputs_filters_by_repository_uid(monkeypatch):
    """`_doc_inputs` must push the tenant predicate into the query rather
    than loading every repo's docs and dropping wrong-repo rows in Python."""
    captured = {}

    class _DocNodes:
        @staticmethod
        async def filter(**kw):
            captured.update(kw)
            return [
                SimpleNamespace(
                    uid="d1",
                    slug="backend/api",
                    title="API",
                    watch_paths=["src/api"],
                ),
            ]

        @staticmethod
        async def all():  # pragma: no cover — must NOT be called
            raise AssertionError(
                "_doc_inputs must call Doc.nodes.filter(...), not .all()"
            )

    import domains.docs.models as docs_models

    monkeypatch.setattr(docs_models.Doc, "nodes", _DocNodes)

    out = await campaign_service._doc_inputs("repo1")

    assert captured == {"repository_uid": "repo1"}
    assert out == [
        {"uid": "d1", "slug": "backend/api", "title": "API", "watch_paths": ["src/api"]}
    ]


async def test_area_map_inputs_filters_by_repository_uid_and_enabled(monkeypatch):
    """Same story for `_area_map_inputs` — Neo4j narrows to the tenant's
    enabled areas instead of a whole-platform scan."""
    captured = {}

    class _AreaNodes:
        @staticmethod
        async def filter(**kw):
            captured.update(kw)
            return []

        @staticmethod
        async def all():  # pragma: no cover — must NOT be called
            raise AssertionError(
                "_area_map_inputs must call Area.nodes.filter(...), not .all()"
            )

    import domains.areas.models as areas_models

    monkeypatch.setattr(areas_models.Area, "nodes", _AreaNodes)

    out = await campaign_service._area_map_inputs("repo1")

    assert captured == {"repository_uid": "repo1", "enabled": True}
    assert out is None  # no rows → no map
