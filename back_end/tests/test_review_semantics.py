"""What counts as a REVIEW — against the real test Neo4j (localhost:7999).

A review is a claim that the artefact was re-checked against the code. Every
write path used to stamp one: renaming a page cleared its stale badge, and so
did retiring an area (which also blanks its spec and scope on the way out, so
it came back "freshly reviewed" and empty).

Only a content-or-anchor change earns the stamp now: Doc body/watch_paths,
Area spec/scope_paths/kind.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from domains.areas.models import Area, AreaEdit, area_is_stale
from domains.areas.schemas import UpdateAreaRequest
from domains.areas.services import area_service
from domains.docs.models import Doc, DocEdit, doc_is_stale
from domains.docs.services import doc_service

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
YESTERDAY = NOW - timedelta(days=1)


def _repo() -> str:
    return "repo-" + uuid4().hex[:8]


async def _stale_doc(repo: str) -> Doc:
    """A page whose watched code moved after its last review."""
    d = Doc(
        uid=uuid4().hex,
        repository_uid=repo,
        slug="backend/queue",
        title="Queue",
        summary="one line",
        body="the body",
        watch_paths=["back_end/queue"],
        code_changed_at=NOW,
        last_reviewed_at=YESTERDAY,
        stale_paths=["back_end/queue/worker.py"],
    )
    await d.save()
    assert doc_is_stale(d)
    return d


async def _stale_area(repo: str, *, spec: str = "the contract") -> Area:
    a = Area(
        uid=uuid4().hex,
        repository_uid=repo,
        key="backend",
        kind="subsystem",
        title="Backend",
        scope_paths=["back_end"],
        spec=spec,
        code_changed_at=NOW,
        last_reviewed_at=YESTERDAY,
        stale_paths=["back_end/app.py"],
    )
    await a.save()
    assert area_is_stale(a)
    return a


# ── Docs ────────────────────────────────────────────────────────────────────


async def test_renaming_a_page_is_not_a_review():
    """Fixing a typo in a title says nothing about whether the prose still
    matches the code — clearing the badge for it is how the signal dies."""
    repo = _repo()
    d = await _stale_doc(repo)

    await doc_service.update_doc(d.uid, title="Queue workers", summary="better")

    after = await Doc.nodes.get(uid=d.uid)
    assert doc_is_stale(after)
    assert after.last_reviewed_at == d.last_reviewed_at
    assert after.stale_paths == ["back_end/queue/worker.py"]
    assert after.title == "Queue workers"  # the edit still landed


async def test_editing_the_body_is_a_review():
    repo = _repo()
    d = await _stale_doc(repo)

    await doc_service.update_doc(d.uid, body="rewritten against the code")

    after = await Doc.nodes.get(uid=d.uid)
    assert not doc_is_stale(after)
    assert after.stale_paths == []


async def test_rewatching_a_page_is_a_review():
    """Re-anchoring to code IS re-deciding what the page covers — and the old
    stale_paths were matched against an anchor that no longer applies."""
    repo = _repo()
    d = await _stale_doc(repo)

    await doc_service.update_doc(d.uid, watch_paths=["back_end/jobs"])

    after = await Doc.nodes.get(uid=d.uid)
    assert not doc_is_stale(after)


async def test_resaving_identical_content_is_not_a_review():
    """The editor PUTs every field on every save, so a no-op save must not
    clear the badge."""
    repo = _repo()
    d = await _stale_doc(repo)

    await doc_service.update_doc(
        d.uid, title=d.title, summary=d.summary, body=d.body,
        watch_paths=list(d.watch_paths),
    )

    after = await Doc.nodes.get(uid=d.uid)
    assert doc_is_stale(after)


async def test_archiving_a_page_is_not_a_review():
    """Retiring a page is not reviewing it — so an un-archive comes back
    truthfully stale rather than falsely current."""
    repo = _repo()
    d = await _stale_doc(repo)
    e = DocEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        doc_uid=d.uid,
        proposed_body=d.body,
        proposed_archived=True,
        status="pending",
    )
    await e.save()

    await doc_service.accept_doc_edit(e.uid)

    after = await Doc.nodes.get(uid=d.uid)
    assert after.archived is True
    assert doc_is_stale(after)
    assert after.last_reviewed_at == d.last_reviewed_at


async def test_accepting_a_content_edit_is_a_review():
    repo = _repo()
    d = await _stale_doc(repo)
    e = DocEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        doc_uid=d.uid,
        proposed_body="a corrected body",
        status="pending",
    )
    await e.save()

    await doc_service.accept_doc_edit(e.uid)

    after = await Doc.nodes.get(uid=d.uid)
    assert not doc_is_stale(after)
    assert after.stale_paths == []


# ── Areas ───────────────────────────────────────────────────────────────────


async def test_renaming_an_area_is_not_a_review():
    repo = _repo()
    a = await _stale_area(repo)

    await area_service.update_area(a.uid, UpdateAreaRequest(title="Back end"))

    after = await Area.nodes.get(uid=a.uid)
    assert area_is_stale(after)
    assert after.title == "Back end"


async def test_editing_the_spec_is_a_review():
    repo = _repo()
    a = await _stale_area(repo)

    await area_service.update_area(a.uid, UpdateAreaRequest(spec="a new contract"))

    after = await Area.nodes.get(uid=a.uid)
    assert not area_is_stale(after)
    assert after.stale_paths == []


async def test_rescoping_an_area_is_a_review():
    repo = _repo()
    a = await _stale_area(repo)

    await area_service.update_area(
        a.uid, UpdateAreaRequest(scope_paths=["back_end/api"])
    )

    after = await Area.nodes.get(uid=a.uid)
    assert not area_is_stale(after)


async def test_disabling_an_area_is_not_a_review():
    """Retirement is not review. It also blanks spec/scope_paths on the accept
    path, so stamping it would bring the area back empty AND falsely current."""
    repo = _repo()
    a = await _stale_area(repo)

    await area_service.update_area(a.uid, UpdateAreaRequest(enabled=False))

    after = await Area.nodes.get(uid=a.uid)
    assert after.enabled is False
    assert area_is_stale(after)
    assert after.last_reviewed_at == a.last_reviewed_at


async def test_accepting_a_retire_edit_is_not_a_review():
    repo = _repo()
    a = await _stale_area(repo)
    e = AreaEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        area_uid=a.uid,
        key=a.key,
        kind=a.kind,
        proposed_spec="",
        scope_paths=[],
        proposed_enabled=False,
        status="pending",
    )
    await e.save()

    await area_service.accept_area_edit(e.uid)

    after = await Area.nodes.get(uid=a.uid)
    assert after.enabled is False
    assert area_is_stale(after)


async def test_accepting_a_content_edit_is_a_review():
    repo = _repo()
    a = await _stale_area(repo)
    e = AreaEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        area_uid=a.uid,
        key=a.key,
        kind=a.kind,
        proposed_spec="a revised contract",
        scope_paths=["back_end"],
        proposed_enabled=True,
        status="pending",
    )
    await e.save()

    await area_service.accept_area_edit(e.uid)

    after = await Area.nodes.get(uid=a.uid)
    assert not area_is_stale(after)


async def test_re_retiring_an_already_disabled_area_is_not_a_review():
    """The retire guard must be absolute, not a transition.

    Two-step: a human toggles `enabled=false` (no stamp, spec/scope intact),
    then an agent's retire AreaEdit is accepted. Because the area is ALREADY
    disabled a transition test reads "not retiring", the full-replacement
    accept blanks spec and scope_paths, the snapshot differs — and the area is
    stamped freshly reviewed while being emptied out.
    """
    repo = _repo()
    a = await _stale_area(repo)
    a.enabled = False
    await a.save()

    e = AreaEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        area_uid=a.uid,
        key=a.key,
        kind=a.kind,
        proposed_spec="",
        scope_paths=[],
        proposed_enabled=False,
        status="pending",
    )
    await e.save()
    await area_service.accept_area_edit(e.uid)

    after = await Area.nodes.get(uid=a.uid)
    assert after.enabled is False
    assert area_is_stale(after)
    assert after.last_reviewed_at == a.last_reviewed_at


async def test_an_edit_that_destroys_the_scope_anchor_is_not_a_review():
    """An area that had scope paths and now has none can never be marked
    stale again — stamping it fresh yields untracked AND confidently current,
    the worst possible row. Agent full-replacements routinely omit
    scope_paths, so this is a common path."""
    repo = _repo()
    a = await _stale_area(repo)
    e = AreaEdit(
        uid=uuid4().hex,
        repository_uid=repo,
        area_uid=a.uid,
        key=a.key,
        kind=a.kind,
        title="Backend",
        proposed_spec="a revised contract",
        scope_paths=[],  # the anchor is dropped on the way past
        proposed_enabled=True,
        status="pending",
    )
    await e.save()
    await area_service.accept_area_edit(e.uid)

    after = await Area.nodes.get(uid=a.uid)
    assert after.scope_paths == []
    assert area_is_stale(after)  # truthfully stale, not falsely fresh


async def test_an_area_that_never_had_a_scope_still_earns_its_stamp():
    """The anchor guard is a TRANSITION, not an absolute — an area that never
    had scope paths is a different thing from one that just lost them."""
    repo = _repo()
    a = Area(
        uid=uuid4().hex,
        repository_uid=repo,
        key="charter",
        kind="subsystem",
        scope_paths=[],
        spec="",
        last_reviewed_at=None,
        stale_paths=[],
    )
    await a.save()

    await area_service.update_area(a.uid, UpdateAreaRequest(spec="a charter"))

    after = await Area.nodes.get(uid=a.uid)
    assert after.last_reviewed_at is not None


async def test_cosmetic_rewrites_are_not_reviews():
    """A trailing newline from an editor, a reordered path list, and a
    duplicated entry are all no-ops that used to clear a stale badge."""
    repo = _repo()

    d = await _stale_doc(repo)
    await doc_service.update_doc(d.uid, body=d.body + "\n\n  ")
    assert doc_is_stale(await Doc.nodes.get(uid=d.uid))

    d2 = await _stale_doc(repo)
    d2.slug = "backend/other"
    d2.watch_paths = ["back_end/a", "back_end/b"]
    await d2.save()
    await doc_service.update_doc(d2.uid, watch_paths=["back_end/b", "back_end/a"])
    assert doc_is_stale(await Doc.nodes.get(uid=d2.uid))

    a = await _stale_area(repo)
    await area_service.update_area(
        a.uid, UpdateAreaRequest(scope_paths=["back_end", "back_end"])
    )
    assert area_is_stale(await Area.nodes.get(uid=a.uid))


# ── Memories ────────────────────────────────────────────────────────────────


async def test_rewriting_a_memory_verbatim_does_not_clear_its_stale_flag():
    """Memory staleness is anchor-doc `code_changed_at` vs the memory's own
    `updated_at`, and the upsert bumped `updated_at` on an identical
    fingerprint — so an agent could clear "code changed since" on every memory
    by re-asserting them all word for word, having checked nothing."""
    from domains.memory.models import Memory
    from domains.memory.services.memory_service import write_memory

    repo = _repo()
    m = Memory(
        uid=uuid4().hex,
        repository_uid=repo,
        title="Queue retries",
        body="Retries are capped at 3.",
        fingerprint="",
        anchor_uid="doc-1",
        updated_at=YESTERDAY,
    )
    # Store the fingerprint the service will compute for this exact content.
    from domains.memory.services.memory_service import _fingerprint

    m.fingerprint = _fingerprint(m.title, m.body)
    await m.save()

    await write_memory(
        repository_uid=repo,
        title="Queue retries",
        body="Retries are capped at 3.",
        anchor_uid="doc-1",
    )

    after = await Memory.nodes.get(uid=m.uid)
    assert after.updated_at == YESTERDAY


async def test_rewriting_a_memory_with_new_content_is_a_review():
    from domains.memory.models import Memory
    from domains.memory.services.memory_service import _fingerprint, write_memory

    repo = _repo()
    m = Memory(
        uid=uuid4().hex,
        repository_uid=repo,
        title="Queue retries",
        body="Retries are capped at 3.",
        fingerprint=_fingerprint("Queue retries", "Retries are capped at 3."),
        anchor_uid="doc-1",
        updated_at=YESTERDAY,
    )
    await m.save()

    await write_memory(
        repository_uid=repo,
        title="Queue retries",
        body="Retries are capped at 5 now.",
        anchor_uid="doc-1",
    )

    after = await Memory.nodes.get(uid=m.uid)
    assert after.updated_at > YESTERDAY
    assert "5" in after.body
