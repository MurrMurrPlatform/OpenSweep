"""Grouping proposals dedupe by CONTAINMENT and refuse free clusters.

Exact-set dedup let one grouping run (or two racing runs) file a proposal, then
file it again minus one member, and bill a separate human review for each.
Separately, an agent re-proposing a cluster the platform computed and showed it
as prior art is a review someone pays for nothing.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.tickets.services import epic_service as svc
from domains.tickets.services.epic_service import EpicService


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch):
    """Everything except the dedup decision is stubbed — the point under test
    is set logic, not persistence."""

    async def _validate(_self, _repo, uids):
        return [SimpleNamespace(uid=u) for u in uids]

    monkeypatch.setattr(
        "domains.tickets.services.ticket_service.TicketService.validate_group_members",
        _validate,
    )

    async def _audit(**_):
        return None

    monkeypatch.setattr(svc, "write_audit", _audit)

    async def _save(self):
        return self

    monkeypatch.setattr(svc.EpicProposal, "save", _save, raising=False)


def _live(uids, uid="existing"):
    return SimpleNamespace(uid=uid, member_ticket_uids=list(uids), status="proposed")


def _patch_existing(monkeypatch, rows):
    async def _filter(**_):
        return rows

    monkeypatch.setattr(svc.EpicProposal, "nodes", SimpleNamespace(filter=_filter))


async def _propose(members, **kw):
    return await EpicService().propose(
        repository_uid="r1", title="T", member_ticket_uids=members, **kw
    )


@pytest.mark.asyncio
async def test_identical_member_set_still_dedupes(monkeypatch):
    _patch_existing(monkeypatch, [_live(["a", "b", "c"])])
    proposal, deduped = await _propose(["c", "b", "a"])
    assert deduped is True
    assert proposal.uid == "existing"


@pytest.mark.asyncio
async def test_a_subset_dedupes_against_the_live_proposal(monkeypatch):
    """The regression: {a,b} proposed while {a,b,c} is already open used to
    create a second card for the same work."""
    _patch_existing(monkeypatch, [_live(["a", "b", "c"])])
    _, deduped = await _propose(["a", "b"])
    assert deduped is True


@pytest.mark.asyncio
async def test_a_superset_dedupes_too(monkeypatch):
    _patch_existing(monkeypatch, [_live(["a", "b"])])
    _, deduped = await _propose(["a", "b", "c"])
    assert deduped is True


@pytest.mark.asyncio
async def test_overlap_without_containment_is_allowed(monkeypatch):
    """{a,b} vs {b,c} is a genuinely different grouping — it must still reach a
    human. Collapsing it here would silently discard a real proposal; the
    already-grouped drop at approval is where that overlap is resolved."""
    _patch_existing(monkeypatch, [_live(["a", "b"])])
    _, deduped = await _propose(["b", "c"])
    assert deduped is False


@pytest.mark.asyncio
async def test_an_empty_live_proposal_never_swallows_everything(monkeypatch):
    """`set() <= anything` is True — without the emptiness guard a malformed
    row with no members would dedupe every future proposal in the repo."""
    _patch_existing(monkeypatch, [_live([])])
    _, deduped = await _propose(["a", "b"])
    assert deduped is False


@pytest.mark.asyncio
async def test_reproposing_a_seeded_cluster_verbatim_is_refused(monkeypatch):
    _patch_existing(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        await _propose(["a", "b"], seeded_cluster_members=[["b", "a"], ["c", "d"]])
    assert exc.value.status_code == 409
    assert "computed clusters" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_extending_a_seeded_cluster_is_allowed(monkeypatch):
    """Extending is the whole point of showing the agent prior art — only the
    verbatim re-proposal is refused."""
    _patch_existing(monkeypatch, [])
    _, deduped = await _propose(["a", "b", "c"], seeded_cluster_members=[["a", "b"]])
    assert deduped is False


@pytest.mark.asyncio
async def test_plan_uid_is_carried_onto_the_proposal(monkeypatch):
    """Without this, agent epics land in the ungrouped bucket and cost one
    approval click each instead of one per plan."""
    _patch_existing(monkeypatch, [])
    proposal, _ = await _propose(["a", "b"], plan_uid="plan-123")
    assert proposal.plan_uid == "plan-123"
