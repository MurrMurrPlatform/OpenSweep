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


async def test_accept_with_empty_scope_paths_clears_them(stores):
    # Full replacement: an edit that carries no scope_paths/doc_uids CLEARS
    # them — key/kind/title are the only keep-if-empty fields.
    a = await area_service.create_area(
        repository_uid="r1",
        key="backend",
        scope_paths=["back_end/"],
        doc_uids=["doc-1"],
    )
    result = await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="scopeless now", key="backend"
    )
    accepted, _warnings = await area_service.accept_area_edit(result["area_edit_uid"])
    assert accepted is a
    assert a.scope_paths == []
    assert a.doc_uids == []
    assert a.key == "backend" and a.kind == "subsystem"  # keep-if-empty fields intact


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
    _, warnings = await area_service.update_area(
        a.uid, UpdateAreaRequest(spec="charter"), actor="human"
    )
    assert a.spec == "charter"
    assert a.last_reviewed_at is not None
    assert a.stale_paths == []
    assert warnings == []


async def test_update_area_warns_on_partition_overlap_of_the_new_values(stores):
    """PATCH warnings mirror accept-time checks, computed over the UPDATED
    scope — the editor sees the overlap they just created."""
    await area_service.create_area(
        repository_uid="r1", key="backend/api", scope_paths=["src/api"]
    )
    a = await area_service.create_area(repository_uid="r1", key="frontend")
    _, warnings = await area_service.update_area(
        a.uid, UpdateAreaRequest(scope_paths=["src/api/views"])
    )
    assert warnings == [
        "scope 'src/api/views' overlaps leaf 'backend/api' ('src/api')"
    ]


async def test_update_area_disabling_clears_the_warning_surface(stores):
    await area_service.create_area(
        repository_uid="r1", key="backend/api", scope_paths=["src/api"]
    )
    a = await area_service.create_area(
        repository_uid="r1", key="frontend", scope_paths=["src/api"]
    )
    _, warnings = await area_service.update_area(
        a.uid, UpdateAreaRequest(enabled=False)
    )
    assert warnings == []  # a disabled area is out of the partition


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


# ── propose-time warnings + retire proposals ────────────────────────────────


async def test_propose_returns_overlap_warnings_against_live_map(stores):
    await area_service.create_area(
        repository_uid="r1", key="backend/api", scope_paths=["back_end/api"]
    )
    result = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="catch-all",
        key="backend-all",
        kind="subsystem",
        scope_paths=["back_end/"],
        source_run_uid="run-1",
    )
    assert any("backend/api" in w for w in result["warnings"])


async def test_propose_warns_against_same_run_pending_proposals(stores):
    # The 86bb524f failure mode: a broad proposal colliding with the same
    # run's earlier specific proposals must warn IN-LOOP, not at accept.
    first = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="router",
        key="frontend-router-views",
        kind="subsystem",
        scope_paths=["front_end/src/router"],
        source_run_uid="run-1",
    )
    assert first["warnings"] == []
    second = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="catch-all",
        key="frontend-app",
        kind="subsystem",
        scope_paths=["front_end/"],
        source_run_uid="run-1",
    )
    assert any("frontend-router-views" in w for w in second["warnings"])
    # Other runs' pending edits are not compared (they may be stale).
    third = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="unrelated",
        key="frontend-assets",
        kind="subsystem",
        scope_paths=["front_end/src/assets"],
        source_run_uid="run-OTHER",
    )
    assert third["warnings"] == []


async def test_propose_slash_children_are_exempt_from_warnings(stores):
    await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="views",
        key="frontend/router-views",
        kind="subsystem",
        scope_paths=["front_end/src/router"],
        source_run_uid="run-1",
    )
    # A proper slash-parent owns no files, so no overlap fires; even a
    # scoped parent is exempt via the key relationship.
    parent = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="grouping",
        key="frontend",
        kind="subsystem",
        scope_paths=[],
        source_run_uid="run-1",
    )
    assert parent["warnings"] == []


async def test_propose_enabled_false_flows_through_accept(stores):
    a = await area_service.create_area(
        repository_uid="r1", key="backend-all", scope_paths=["back_end/"]
    )
    result = await area_service.propose_area_edit(
        repository_uid="r1",
        proposed_spec="retire: replaced by backend/* leaves",
        key="backend-all",
        kind="subsystem",
        enabled=False,
        source_run_uid="run-1",
    )
    # A retire proposal is not warned about — it removes overlap.
    assert result["warnings"] == []
    updated, _warnings = await area_service.accept_area_edit(
        result["area_edit_uid"], actor="human"
    )
    assert updated.uid == a.uid
    assert updated.enabled is False


async def test_reset_areas_wipes_areas_and_edits_for_the_repo_only(stores):
    await area_service.create_area(repository_uid="r1", key="backend")
    await area_service.create_area(repository_uid="r1", key="frontend")
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="x", key="new-area", source_run_uid="run-1"
    )
    other = await area_service.create_area(repository_uid="r2", key="backend")
    result = await area_service.reset_areas("r1", actor="human")
    assert result == {"areas_deleted": 2, "edits_deleted": 1}
    assert stores.areas == [other]
    assert stores.edits == []


# ── area_detail — the one-load detail payload ───────────────────────────────


@pytest.fixture
def detail_seams(stores, monkeypatch) -> SimpleNamespace:
    """area_detail's lazy-import seams stubbed: repo + tree, docs, and
    Checked stamps live in this namespace; Areas/AreaEdits ride `stores`."""
    import domains.checked.services.checked_service as checked_service
    import domains.docs.services.doc_service as doc_service
    import domains.repositories.models as repo_models
    import domains.repositories.services.file_tree as file_tree_mod

    state = SimpleNamespace(
        tree=([], ""),
        docs=[],  # SimpleNamespace(uid, slug, title, watch_paths)
        stamps=[],
        stamp_calls=[],
    )

    class _RepoNodes:
        @staticmethod
        async def get_or_none(**kw):
            return SimpleNamespace(uid=kw.get("uid"))

    monkeypatch.setattr(
        repo_models, "Repository", SimpleNamespace(nodes=_RepoNodes)
    )

    async def fake_tree(repo):
        return state.tree

    monkeypatch.setattr(file_tree_mod, "file_tree_paths", fake_tree)

    async def fake_get_doc(uid):
        for d in state.docs:
            if d.uid == uid:
                return d
        raise HTTPException(status_code=404, detail="gone")

    async def fake_list_docs(repository_uid):
        return list(state.docs)

    monkeypatch.setattr(doc_service, "get_doc", fake_get_doc)
    monkeypatch.setattr(doc_service, "list_docs", fake_list_docs)

    async def fake_stamps(repository_uid, paths, *, limit=10):
        state.stamp_calls.append((repository_uid, list(paths), limit))
        return list(state.stamps)

    monkeypatch.setattr(checked_service, "stamps_for_paths", fake_stamps)
    return state


async def test_area_detail_sizes_scope_against_the_tree(stores, detail_seams):
    a = await area_service.create_area(
        repository_uid="r1", key="backend", scope_paths=["src/api", "gone/dir"]
    )
    detail_seams.tree = (["src/api/a.py", "src/api/b.py", "other.md"], "")

    out = await area_service.area_detail(a.uid)

    assert out.area.uid == a.uid
    assert out.tree_degraded == ""
    by_path = {s.path: s for s in out.scope}
    assert by_path["src/api"].file_count == 2
    assert by_path["src/api"].dead is False
    assert by_path["src/api"].files == ["src/api/a.py", "src/api/b.py"]
    assert by_path["gone/dir"].file_count == 0
    assert by_path["gone/dir"].dead is True
    assert by_path["gone/dir"].files == []


async def test_area_detail_scope_files_cap_at_50(stores, detail_seams):
    a = await area_service.create_area(
        repository_uid="r1", key="backend", scope_paths=["src"]
    )
    detail_seams.tree = ([f"src/f{i:03}.py" for i in range(80)], "")
    out = await area_service.area_detail(a.uid)
    (entry,) = out.scope
    assert entry.file_count == 80
    assert len(entry.files) == 50


async def test_area_detail_degraded_tree_never_declares_scopes_dead(
    stores, detail_seams
):
    a = await area_service.create_area(
        repository_uid="r1", key="backend", scope_paths=["src/api"]
    )
    detail_seams.tree = ([], "no active git provider connection")
    out = await area_service.area_detail(a.uid)
    assert out.tree_degraded == "no active git provider connection"
    (entry,) = out.scope
    assert entry.file_count is None
    assert entry.dead is False


async def test_area_detail_links_and_suggests_docs(stores, detail_seams):
    a = await area_service.create_area(
        repository_uid="r1",
        key="backend",
        scope_paths=["src/api"],
        doc_uids=["d1", "d-deleted"],
    )
    detail_seams.docs = [
        SimpleNamespace(uid="d1", slug="api", title="API", watch_paths=["elsewhere"]),
        SimpleNamespace(
            uid="d2", slug="api-deep", title="API deep", watch_paths=["src/api/deep"]
        ),
        SimpleNamespace(uid="d3", slug="fe", title="FE", watch_paths=["front_end"]),
    ]
    out = await area_service.area_detail(a.uid)
    # Linked: best-effort — the deleted uid is skipped, not an error.
    assert [d.uid for d in out.linked_docs] == ["d1"]
    assert out.linked_docs[0].slug == "api"
    # Suggested: watch overlap with the scope, minus already-linked.
    assert [d.uid for d in out.suggested_docs] == ["d2"]


async def test_area_detail_relates_features_and_subsystem_leaves(
    stores, detail_seams
):
    sub = await area_service.create_area(
        repository_uid="r1", key="backend/api", scope_paths=["src/api"]
    )
    # A parent grouping never appears as related — only leaves.
    await area_service.create_area(
        repository_uid="r1", key="backend", scope_paths=[]
    )
    feat = await area_service.create_area(
        repository_uid="r1",
        key="checkout",
        kind="feature",
        scope_paths=["src/api/checkout.py", "front_end/checkout"],
    )
    await area_service.create_area(
        repository_uid="r1", key="frontend", scope_paths=["front_end"]
    )

    sub_out = await area_service.area_detail(sub.uid)
    assert [r.uid for r in sub_out.related_areas] == [feat.uid]
    assert sub_out.related_areas[0].kind == "feature"

    feat_out = await area_service.area_detail(feat.uid)
    related_keys = {r.key for r in feat_out.related_areas}
    assert related_keys == {"backend/api", "frontend"}


async def test_area_detail_coverage_and_pending_edits(stores, detail_seams):
    a = await area_service.create_area(
        repository_uid="r1", key="backend", scope_paths=["src"]
    )
    detail_seams.stamps = [
        SimpleNamespace(
            run_uid="run-9",
            outcome="findings",
            checked_at=None,
            lens_verdicts=[{"lens": "bugs", "verdict": "checked-findings"}, "junk"],
        )
    ]
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="backend", source_run_uid="run-1"
    )
    await area_service.propose_area_edit(
        repository_uid="r1", proposed_spec="s", key="unrelated", source_run_uid="run-1"
    )

    out = await area_service.area_detail(a.uid)

    assert detail_seams.stamp_calls == [("r1", ["src"], 10)]
    (cov,) = out.coverage
    assert cov.run_uid == "run-9" and cov.outcome == "findings"
    assert cov.lens_verdicts == [{"lens": "bugs", "verdict": "checked-findings"}]
    # Pending edits: only THIS area's; the badge count matches the list.
    assert [e.area_uid for e in out.pending_edits] == [a.uid]
    assert out.area.pending_edits == 1
