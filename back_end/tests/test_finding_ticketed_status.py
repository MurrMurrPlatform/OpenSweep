"""Promoting a finding takes it off the open board — status "ticketed".

WHY: converting a finding to a ticket used to leave the finding `open`, so it
sat on the board indistinguishable from untouched work and could be promoted
again and again. Triage already moved a finding out of the default view;
conversion was the one action that did not.

WHAT this pins:
  - creating a ticket from an open finding sets status="ticketed" + ticket_uid;
  - it NEVER overwrites an existing triage decision (so the first ticket wins);
  - deleting that ticket releases the finding back to `open`, but only while
    the finding still points at it;
  - `status="processed"` on the list endpoint means "everything not open".

DB-free with in-memory fakes, matching test_ticket_reference_tenancy.py.
"""

import pytest

import domains.findings.services.finding_service as finding_mod
import domains.tickets.services.ticket_service as ticket_mod
from domains.findings.services.finding_service import STATUS_PROCESSED, FindingService
from domains.tickets.schemas import CreateTicketRequest
from domains.tickets.services.ticket_service import TicketService

pytestmark = pytest.mark.asyncio

_STORE: dict[str, list] = {}


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        store = _STORE.setdefault(type(self).__name__, [])
        if self not in store:
            store.append(self)
        return self

    async def delete(self):
        _STORE.get(type(self).__name__, []).remove(self)


def _nodes_for(store_key: str):
    class _Nodes:
        async def get_or_none(self, **kw):
            for n in _STORE.get(store_key, []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        async def filter(self, **kw):
            return [
                n
                for n in _STORE.get(store_key, [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]

        async def all(self):
            return list(_STORE.get(store_key, []))

    return _Nodes()


class FakeTicket(_Node):
    nodes = _nodes_for("FakeTicket")
    origin_finding_uid = ""
    status = "backlog"


class FakeFinding(_Node):
    """Enough of the node to survive `finding_to_dto`, which the list path
    runs on every result — the DTO validates kind/severity/size enums, so
    blank defaults would fail validation rather than model the real node."""

    nodes = _nodes_for("FakeFinding")

    title = "a finding"
    kind = "defect"
    severity = "medium"
    size = "medium"
    subtype = ""
    tags: list[str] = []
    confidence = 0.7
    description = ""
    root_cause = ""
    why_it_matters = ""
    evidence: dict = {}
    suggested_fix = ""
    affected_paths: list[str] = []
    dedupe_key = "dk"
    source_run_uid = None
    source_run_uids: list[str] = []
    last_confirmed_at = None
    executor = "manual"
    source_path = "tool-call"
    parse_status = "ok"
    detected_by_tool = ""
    detected_by_rule = ""
    status = "open"
    ticket_uid = ""
    created_at = None
    updated_at = None


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(ticket_mod, "Ticket", FakeTicket)
    monkeypatch.setattr(ticket_mod, "Finding", FakeFinding, raising=False)
    monkeypatch.setattr(ticket_mod, "write_audit", no_audit)
    monkeypatch.setattr(finding_mod, "Finding", FakeFinding)
    monkeypatch.setattr(finding_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _seed_finding(uid, *, status="open", ticket_uid="", repo="repo-a"):
    n = FakeFinding(uid=uid, repository_uid=repo, status=status, ticket_uid=ticket_uid, tags=[])
    _STORE.setdefault("FakeFinding", []).append(n)
    return n


async def _promote(finding_uid, repo="repo-a"):
    return await TicketService().create(
        CreateTicketRequest(title="t", repository_uid=repo, origin_finding_uid=finding_uid)
    )


# ── Promotion ────────────────────────────────────────────────────────────────


async def test_promoting_an_open_finding_marks_it_ticketed_and_links_forward():
    f = _seed_finding("f1")
    ticket = await _promote("f1")
    assert f.status == "ticketed"
    assert f.ticket_uid == ticket.uid


async def test_promotion_never_overwrites_an_existing_triage_decision():
    # Someone judged this won't-fix. Promoting it anyway must not relabel it
    # "ticketed" — that would silently discard a human's call.
    f = _seed_finding("f1", status="wont-fix")
    await _promote("f1")
    assert f.status == "wont-fix"
    assert f.ticket_uid == ""


async def test_the_first_ticket_wins_the_link():
    f = _seed_finding("f1")
    first = await _promote("f1")
    second = await _promote("f1")
    assert first.uid != second.uid
    assert f.ticket_uid == first.uid, "a second promotion must not steal the link"


# ── Release on ticket deletion ───────────────────────────────────────────────


async def test_deleting_the_ticket_returns_the_finding_to_the_board():
    f = _seed_finding("f1")
    ticket = await _promote("f1")
    await TicketService().delete(ticket.uid)
    assert f.status == "open"
    assert f.ticket_uid == ""


async def test_deleting_the_ticket_leaves_a_since_triaged_finding_alone():
    # Promoted, then marked fixed by hand. Deleting the ticket must not drag it
    # back onto the board — "fixed" is the newer, human decision. The dead
    # LINK still has to go, though: see the next test.
    f = _seed_finding("f1")
    ticket = await _promote("f1")
    f.status = "fixed"
    await TicketService().delete(ticket.uid)
    assert f.status == "fixed"


async def test_deleting_the_ticket_always_drops_the_link_even_when_status_stays():
    # Promote → triage → delete the ticket. The status legitimately stays
    # "fixed", but keeping ticket_uid would render a "Became a ticket →" link
    # to a ticket that no longer exists AND permanently disable re-promotion
    # (the button is gated on !ticket_uid).
    f = _seed_finding("f1")
    ticket = await _promote("f1")
    f.status = "fixed"
    await TicketService().delete(ticket.uid)
    assert f.ticket_uid == "", "a link must never outlive the ticket it points at"


async def test_a_released_finding_can_be_promoted_again():
    f = _seed_finding("f1")
    first = await _promote("f1")
    await TicketService().delete(first.uid)
    assert f.status == "open" and f.ticket_uid == ""
    second = await _promote("f1")
    assert f.status == "ticketed"
    assert f.ticket_uid == second.uid


async def test_clear_ticket_ignores_a_link_that_moved_on():
    f = _seed_finding("f1", status="ticketed", ticket_uid="ticket-current")
    moved = await FindingService().clear_ticket("f1", ticket_uid="ticket-stale")
    assert moved is False
    assert f.ticket_uid == "ticket-current"


async def test_clear_ticket_tolerates_a_deleted_finding():
    # Findings are deletable. Deleting the ticket of one that is already gone
    # must not raise — it would make the ticket undeletable.
    assert await FindingService().clear_ticket("gone", ticket_uid="t1") is False


# ── The "processed" pseudo-status ────────────────────────────────────────────


async def test_processed_returns_everything_except_open():
    _seed_finding("open1")
    _seed_finding("t1", status="ticketed")
    _seed_finding("ack1", status="acknowledged")
    # `accepted` is unreachable from the board's individual status options, so
    # "processed" is the only way to see it — pin that it is included.
    _seed_finding("acc1", status="accepted")

    got = await FindingService().list(repository_uid="repo-a", status=STATUS_PROCESSED)
    assert {f.uid for f in got} == {"t1", "ack1", "acc1"}


async def test_an_exact_status_still_filters_exactly():
    _seed_finding("t1", status="ticketed")
    _seed_finding("ack1", status="acknowledged")
    got = await FindingService().list(repository_uid="repo-a", status="ticketed")
    assert {f.uid for f in got} == {"t1"}


async def test_no_status_filter_returns_open_and_processed_alike():
    _seed_finding("open1")
    _seed_finding("t1", status="ticketed")
    got = await FindingService().list(repository_uid="repo-a")
    assert {f.uid for f in got} == {"open1", "t1"}


# ── "Processed" ≠ "resolved" ─────────────────────────────────────────────────


async def test_ticketed_is_processed_for_the_board_but_still_outstanding_work():
    """The subtle invariant this feature introduces.

    "Processed" is a BOARD concern — has a human dealt with this yet — and it
    hides `ticketed`. Every other consumer asks a different question: is this
    problem still real? A ticket in flight does not fix anything, so the
    dashboard counters and the agent-facing prior-findings lookup must keep
    counting it. Conflating the two questions is how promoting a finding would
    silently drop the org's open-issue count.
    """
    from domains.metrics.services.metrics_service import _OPEN_FINDING_STATUSES
    from domains.platform_tools.prior_findings import prior_findings

    assert "ticketed" in _OPEN_FINDING_STATUSES
    assert "ticketed" in prior_findings.__kwdefaults__["statuses"]
