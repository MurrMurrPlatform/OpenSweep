"""Ticket archive: model fields, DTO passthrough, guard matrix, route surface.

Archive is the reversible "leave the board" for any status; delete stays
backlog-only. `ensure_archivable` is pure precisely so this matrix runs
without Neo4j.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import app
from domains.tickets.models import Ticket
from domains.tickets.services.ticket_service import ensure_archivable, ticket_to_dto


def _ticket(**over):
    base = dict(
        uid="t-1", repository_uid="r-1", title="T", description="",
        acceptance_criteria=[], labels=[], status="todo", priority="medium",
        size="", severity="", kind="", tags=[], subtype="",
        origin="human", origin_finding_uid="", parent_ticket_uid="",
        linked_finding_uids=[], linked_pr_uids=[], assignee_uid="",
        plan={}, approved_by="", approved_at=None, done_at=None,
        created_at=None, updated_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _thread(phase: str) -> SimpleNamespace:
    return SimpleNamespace(uid="th-1", phase=phase)


# ── Model + DTO ──────────────────────────────────────────────────────────────


def test_ticket_has_archive_fields():
    props = Ticket.defined_properties(rels=False, aliases=False)
    assert {"archived", "archived_at", "archived_by"} <= set(props)


def test_dto_carries_archived():
    dto = ticket_to_dto(_ticket(archived=True, archived_at=None, archived_by="u-9"))
    assert dto.archived is True
    assert dto.archived_by == "u-9"


def test_dto_defaults_archived_false_when_absent():
    # Pre-m0021 nodes (and old test doubles) have no `archived` attribute.
    dto = ticket_to_dto(_ticket())
    assert dto.archived is False
    assert dto.archived_by == ""


# ── ensure_archivable matrix ─────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["backlog", "todo", "in-progress", "in-review", "done"])
def test_archivable_from_every_status(status):
    ensure_archivable(_ticket(status=status), threads=[], children=[])


@pytest.mark.parametrize("phase", ["refining", "implementing", "in_review"])
def test_active_thread_blocks_archive(phase):
    with pytest.raises(HTTPException) as exc:
        ensure_archivable(_ticket(), threads=[_thread(phase)], children=[])
    assert exc.value.status_code == 409
    # The message names the thread — Abandon is the obvious fix.
    assert "th-1" in exc.value.detail


def test_terminal_threads_do_not_block_archive():
    ensure_archivable(
        _ticket(), threads=[_thread("done"), _thread("abandoned")], children=[]
    )


def test_epic_member_archives_through_parent():
    with pytest.raises(HTTPException) as exc:
        ensure_archivable(_ticket(parent_ticket_uid="t-parent"), threads=[], children=[])
    assert exc.value.status_code == 409
    assert "epic" in exc.value.detail


def test_epic_parent_with_live_child_blocks_archive():
    with pytest.raises(HTTPException) as exc:
        ensure_archivable(_ticket(), threads=[], children=[_ticket(status="todo")])
    assert exc.value.status_code == 409


def test_epic_parent_with_done_or_archived_children_archives():
    ensure_archivable(
        _ticket(),
        threads=[],
        children=[_ticket(status="done"), _ticket(status="todo", archived=True)],
    )


# ── Route surface ────────────────────────────────────────────────────────────


def test_archive_routes_are_mounted():
    spec = app.openapi()
    paths = spec.get("paths", {})
    assert "/api/v1/tickets/{uid}/archive" in paths
    assert "/api/v1/tickets/{uid}/unarchive" in paths
    # GET /tickets grew the archived query param (default false).
    params = {p["name"] for p in paths["/api/v1/tickets"]["get"].get("parameters", [])}
    assert "archived" in params
