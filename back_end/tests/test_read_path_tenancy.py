"""Regression: cross-org reads must return 404 (never data) for org-a
resources when the caller belongs to org-b.

DB-free — Run.nodes.get_or_none and require_repo_in_org are both faked.
Each test pins that the tenancy guard fires BEFORE the downstream data read."""

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user():
    from domains.users.schemas import UserDTO

    return UserDTO(uid="u", email="e@x.y", display_name="U", role="maintainer", org_uid="org-a")


def _fake_run(repo_uid: str = "repo-in-org-b"):
    """Minimal fake Run node — only the fields the handlers inspect."""

    class _FakeRun:
        uid = "run-1"
        status = "ended"

    run = _FakeRun()
    run.repository_uid = repo_uid
    return run


# ---------------------------------------------------------------------------
# test_get_run: GET /api/v1/runs/{uid}
# ---------------------------------------------------------------------------

async def test_get_run_foreign_org_raises_404(monkeypatch):
    import api.v1.runs as runs

    calls: list[tuple[str, str]] = []

    async def fake_require(repo, org):
        calls.append((repo, org))
        raise HTTPException(status_code=404, detail="not found")

    async def fake_reconcile():
        pass

    class _FakeNodes:
        async def get_or_none(self, **kwargs):
            return _fake_run()

    async def boom_run_to_dto(r):
        raise AssertionError("run_to_dto reached despite cross-org repo")

    monkeypatch.setattr(runs, "require_repo_in_org", fake_require)
    monkeypatch.setattr(runs, "reconcile_stale_runs", fake_reconcile)
    monkeypatch.setattr(runs.Run, "nodes", _FakeNodes())
    monkeypatch.setattr(runs, "run_to_dto", boom_run_to_dto)

    user = _user()

    with pytest.raises(HTTPException) as exc:
        await runs.get_run(uid="run-1", user=user)

    assert exc.value.status_code == 404
    assert calls == [("repo-in-org-b", "org-a")]


# ---------------------------------------------------------------------------
# test_get_transcript: GET /api/v1/runs/{uid}/transcript
# ---------------------------------------------------------------------------

async def test_get_transcript_foreign_org_raises_404(monkeypatch):
    import api.v1.runs as runs

    calls: list[tuple[str, str]] = []

    async def fake_require(repo, org):
        calls.append((repo, org))
        raise HTTPException(status_code=404, detail="not found")

    class _FakeNodes:
        async def get_or_none(self, **kwargs):
            return _fake_run()

    def boom_read_events(uid, after_seq):
        raise AssertionError("read_events reached despite cross-org repo")

    monkeypatch.setattr(runs, "require_repo_in_org", fake_require)
    monkeypatch.setattr(runs.Run, "nodes", _FakeNodes())
    monkeypatch.setattr(runs, "read_events", boom_read_events)

    user = _user()

    with pytest.raises(HTTPException) as exc:
        await runs.get_transcript(uid="run-1", after_seq=0, user=user)

    assert exc.value.status_code == 404
    assert calls == [("repo-in-org-b", "org-a")]


# ---------------------------------------------------------------------------
# test_get_run_changes: GET /api/v1/runs/{uid}/changes
# ---------------------------------------------------------------------------

async def test_get_run_changes_foreign_org_raises_404(monkeypatch):
    import api.v1.runs as runs

    calls: list[tuple[str, str]] = []

    async def fake_require(repo, org):
        calls.append((repo, org))
        raise HTTPException(status_code=404, detail="not found")

    class _FakeNodes:
        async def get_or_none(self, **kwargs):
            return _fake_run()

    async def boom_read_changes(r):
        raise AssertionError("read_changes reached despite cross-org repo")

    monkeypatch.setattr(runs, "require_repo_in_org", fake_require)
    monkeypatch.setattr(runs.Run, "nodes", _FakeNodes())
    monkeypatch.setattr(runs, "read_changes", boom_read_changes)

    user = _user()

    with pytest.raises(HTTPException) as exc:
        await runs.get_run_changes(uid="run-1", user=user)

    assert exc.value.status_code == 404
    assert calls == [("repo-in-org-b", "org-a")]


# ---------------------------------------------------------------------------
# test_get_artifact: GET /api/v1/artifacts?uri=...
# ---------------------------------------------------------------------------

async def test_get_artifact_foreign_org_raises_404(monkeypatch):
    import api.v1.artifacts as artifacts

    calls: list[tuple[str, str]] = []

    async def fake_require(repo, org):
        calls.append((repo, org))
        raise HTTPException(status_code=404, detail="not found")

    def boom_artifact_get(uri):
        raise AssertionError("artifact_store.get reached despite cross-org repo")

    monkeypatch.setattr(artifacts, "require_repo_in_org", fake_require)
    monkeypatch.setattr(artifacts.artifact_store, "get", boom_artifact_get)

    user = _user()
    uri = "opensweep-artifact://repo-in-org-b/run-1/raw_transcript.txt"

    with pytest.raises(HTTPException) as exc:
        await artifacts.get_artifact(uri=uri, user=user)

    assert exc.value.status_code == 404
    # artifact_store.repository_uid_of applies _safe normalization, but for a
    # simple alphanumeric repo uid the result is identical.
    assert calls == [("repo-in-org-b", "org-a")]
