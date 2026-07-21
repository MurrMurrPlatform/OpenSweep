"""Area service lifecycle — DB-free (Area/AreaEdit faked, audit no-op).

Mirrors the test_finding_dedupe.py fake-node approach: the service's
module-level Area/AreaEdit/write_audit names are monkeypatched so the
propose → accept/reject flow, the per-(area, run) dedupe, and the delete
cleanup run against in-memory stores.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.areas.schemas import UpdateAreaRequest
from domains.areas.services import area_service


class _FakeNodes:
    def __init__(self, store: list):
        self._store = store

    async def all(self):
        return list(self._store)

    async def get_or_none(self, **kwargs):
        for n in self._store:
            if all(getattr(n, k, None) == v for k, v in kwargs.items()):
                return n
        return None


def _make_fake_node_class(store: list, defaults: dict):
    class FakeNode:
        nodes = _FakeNodes(store)

        def __init__(self, **kwargs):
            fields = dict(defaults)
            fields.update(kwargs)
            self.__dict__.update(fields)

        async def save(self):
            if self not in store:
                store.append(self)

        async def delete(self):
            if self in store:
                store.remove(self)

    return FakeNode


_AREA_DEFAULTS = dict(
    uid="",
    repository_uid="",
    key="",
    kind="subsystem",
    title="",
    scope_paths=[],
    spec="",
    doc_uids=[],
    enabled=True,
    provenance="system",
    code_changed_at=None,
    last_reviewed_at=None,
    stale_paths=[],
    created_at=None,
    updated_at=None,
)

_EDIT_DEFAULTS = dict(
    uid="",
    repository_uid="",
    area_uid="",
    key="",
    kind="",
    title="",
    scope_paths=[],
    doc_uids=[],
    proposed_spec="",
    rationale="",
    source_run_uid="",
    status="pending",
    resolved_by="",
    resolved_at=None,
    created_at=None,
)


async def _noop_write_audit(**kwargs):
    return None


@pytest.fixture
def stores(monkeypatch) -> SimpleNamespace:
    areas: list = []
    edits: list = []
    monkeypatch.setattr(
        area_service, "Area", _make_fake_node_class(areas, _AREA_DEFAULTS)
    )
    monkeypatch.setattr(
        area_service, "AreaEdit", _make_fake_node_class(edits, _EDIT_DEFAULTS)
    )
    monkeypatch.setattr(area_service, "write_audit", _noop_write_audit)
    return SimpleNamespace(areas=areas, edits=edits)


async def test_propose_creates_pending_edit_for_new_area(stores):
    result = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="## Convergence\nowns the ledger",
        key="Backend/Delivery Convergence",
        kind="subsystem",
        source_run_uid="run-1",
    )
    assert result["status"] == "ok"
    assert result["new_area"] is True
    assert result["area_uid"] == ""
    (e,) = stores.edits
    assert e.uid == result["area_edit_uid"]
    assert e.status == "pending"
    assert e.key == "backend/delivery-convergence"  # normalized


async def test_propose_resolves_existing_area_by_key(stores):
    a = await area_service.create_area(repository_uid="r1", key="backend")
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="new spec", key="backend"
    )
    assert result["new_area"] is False
    assert result["area_uid"] == a.uid


async def test_propose_dedupes_per_area_and_run(stores):
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="first", key="backend", source_run_uid="run-1"
    )
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="second", key="backend", source_run_uid="run-1"
    )
    # A second proposal from the same run replaces the first — never a duplicate.
    (e,) = stores.edits
    assert e.proposed_spec == "second"
    # A different run keeps its own pending edit.
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="third", key="backend", source_run_uid="run-2"
    )
    assert len(stores.edits) == 2


async def test_propose_rejects_empty_key_and_unknown_kind(stores):
    with pytest.raises(HTTPException) as exc:
        await area_service.propose_area_edit(
            repository_uid="r1", proposed_spec="s", key="//"
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await area_service.propose_area_edit(
            repository_uid="r1", proposed_spec="s", key="backend", kind="bogus"
        )
    assert exc.value.status_code == 422
    assert stores.edits == []


async def test_accept_applies_full_replacement_and_stamps_review(stores):
    a = await area_service.create_area(
        repository_uid="r1", key="backend", spec="old", title="Backend"
    )
    a.code_changed_at = None
    a.last_reviewed_at = None
    a.stale_paths = ["back_end/old.py"]
    result = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="new spec",
        key="backend",
        kind="feature",
        title="Backend Core",
        scope_paths=["back_end/"],
        doc_uids=["doc-1"],
    )
    accepted, warnings = await area_service.accept_area_edit(
        result["area_edit_uid"], actor="reviewer"
    )
    assert accepted is a
    assert a.spec == "new spec"
    assert a.kind == "feature"
    assert a.title == "Backend Core"
    assert a.scope_paths == ["back_end/"]
    assert a.doc_uids == ["doc-1"]
    assert a.last_reviewed_at is not None  # an accepted edit counts as a review
    assert a.stale_paths == []
    assert warnings == []  # feature areas never warn
    (e,) = stores.edits
    assert e.status == "accepted" and e.resolved_by == "reviewer"


async def test_accept_creates_area_for_new_area_proposal(stores):
    result = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="",
        key="vendored",
        kind="ignore",
        scope_paths=["third_party/"],
    )
    a, warnings = await area_service.accept_area_edit(result["area_edit_uid"])
    assert a.key == "vendored" and a.kind == "ignore" and a.provenance == "agent"
    assert a in stores.areas
    # An empty ignore spec is accepted but flagged for the reviewer.
    assert any("without a reason" in w for w in warnings)


async def test_accept_non_pending_edit_is_409(stores):
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="backend"
    )
    await area_service.accept_area_edit(result["area_edit_uid"])
    with pytest.raises(HTTPException) as exc:
        await area_service.accept_area_edit(result["area_edit_uid"])
    assert exc.value.status_code == 409


async def test_accept_new_area_with_now_conflicting_key_is_409(stores):
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="backend"
    )
    # The key was claimed between proposal and review.
    await area_service.create_area(repository_uid="r1", key="backend")
    with pytest.raises(HTTPException) as exc:
        await area_service.accept_area_edit(result["area_edit_uid"])
    assert exc.value.status_code == 409


async def test_reject_marks_edit_rejected(stores):
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="backend"
    )
    dto = await area_service.reject_area_edit(result["area_edit_uid"], actor="reviewer")
    assert dto.status == "rejected"
    assert dto.resolved_by == "reviewer"
    with pytest.raises(HTTPException) as exc:
        await area_service.reject_area_edit(result["area_edit_uid"])
    assert exc.value.status_code == 409


async def test_update_area_counts_as_review(stores):
    a = await area_service.create_area(repository_uid="r1", key="backend")
    a.last_reviewed_at = None
    a.stale_paths = ["back_end/models.py"]
    await area_service.update_area(a.uid, UpdateAreaRequest(spec="charter"), actor="human")
    assert a.spec == "charter"
    assert a.last_reviewed_at is not None
    assert a.stale_paths == []


async def test_delete_area_rejects_pending_edits(stores):
    a = await area_service.create_area(repository_uid="r1", key="backend")
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="backend"
    )
    await area_service.delete_area(a.uid, actor="human")
    assert stores.areas == []
    (e,) = stores.edits
    assert e.uid == result["area_edit_uid"]
    assert e.status == "rejected" and e.resolved_by == "human"


async def test_list_areas_sorts_by_key_and_counts_pending_edits(stores):
    b = await area_service.create_area(repository_uid="r1", key="frontend")
    await area_service.create_area(repository_uid="r1", key="backend")
    await area_service.create_area(repository_uid="r2", key="other-repo")
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="frontend"
    )
    dtos = await area_service.list_areas("r1")
    assert [d.key for d in dtos] == ["backend", "frontend"]
    assert [d.pending_edits for d in dtos] == [0, 1]
    assert dtos[1].uid == b.uid
