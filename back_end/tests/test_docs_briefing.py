"""build_briefing: pinned pages inline verbatim; the run's target (linked
doc_uids + watch-path overlap with target paths) gets a likely-relevant
LISTING — docs are leads the agent pulls with read_doc, never force-fed;
everything else lands in the grouped index."""

from types import SimpleNamespace

import domains.docs.services.briefing as briefing
from domains.docs.services.briefing import build_briefing


def _doc(
    uid,
    slug,
    *,
    repo="r1",
    title="",
    summary="",
    body="",
    pinned=False,
    watch_paths=(),
):
    return SimpleNamespace(
        uid=uid,
        repository_uid=repo,
        slug=slug,
        title=title or slug,
        summary=summary,
        body=body,
        pinned=pinned,
        archived=False,
        watch_paths=list(watch_paths),
        code_changed_at=None,
        last_reviewed_at=None,
    )


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def filter(self, **kwargs):
        return [
            r
            for r in self._rows
            if all(getattr(r, k, None) == v for k, v in kwargs.items())
        ]


async def _no_memories(**_kwargs):
    return []


def _patch(monkeypatch, docs):
    monkeypatch.setattr(briefing, "Doc", SimpleNamespace(nodes=_Nodes(docs)))
    monkeypatch.setattr(briefing, "search_memory", _no_memories)


async def test_pinned_inlined_verbatim_targets_only_listed(monkeypatch):
    _patch(
        monkeypatch,
        [
            _doc("d1", "conventions", body="Pinned body text", pinned=True),
            _doc("d2", "backend/api", body="API body", watch_paths=["src/api"]),
            _doc("d3", "frontend/views", body="FE body", watch_paths=["front"]),
        ],
    )
    out = await build_briefing(repository_uid="r1", target_doc_uids=["d2"])
    assert "Pinned body text" in out
    # The targeted page is listed as likely relevant, NOT inlined.
    assert "API body" not in out
    assert "## Docs likely relevant to this run's scope" in out
    assert "- backend/api" in out.split("## Other documentation pages")[0]
    # The unrelated page only appears in the read_doc index.
    assert "frontend/views" in out.split("## Other documentation pages")[1]


async def test_watch_path_overlap_with_target_paths_is_likely_relevant(monkeypatch):
    _patch(
        monkeypatch,
        [
            _doc("d1", "backend/api", watch_paths=["src/api"]),
            _doc("d2", "backend/workers", watch_paths=["src/workers"]),
        ],
    )
    # Overlap is "/"-boundary in either direction: scope inside the watch
    # path or watch path inside the scope.
    out = await build_briefing(repository_uid="r1", target_paths=["src/api/handlers"])
    relevant = out.split("## Other documentation pages")[0]
    assert "## Docs likely relevant to this run's scope" in relevant
    assert "backend/api" in relevant
    assert "backend/workers" not in relevant


async def test_no_target_means_no_likely_relevant_section(monkeypatch):
    _patch(monkeypatch, [_doc("d1", "backend/api", watch_paths=["src/api"])])
    out = await build_briefing(repository_uid="r1")
    assert "likely relevant" not in out
    assert "## Other documentation pages" in out


async def test_other_repo_and_archived_docs_are_excluded(monkeypatch):
    other = _doc("d9", "elsewhere", repo="r2", watch_paths=["src"])
    archived = _doc("d8", "old-page", watch_paths=["src"])
    archived.archived = True
    _patch(monkeypatch, [other, archived, _doc("d1", "live", watch_paths=["src"])])
    out = await build_briefing(repository_uid="r1", target_paths=["src"])
    assert "elsewhere" not in out
    assert "old-page" not in out
    assert "live" in out
