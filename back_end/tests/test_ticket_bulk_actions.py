"""Bulk ticket actions: per-uid tenancy, partial success, no short-circuit.

Copied deliberately from `EpicService.bulk_approve`'s contract. The two
properties that matter are the two that are easy to get wrong: tenancy must be
re-checked for EVERY uid (the list is client-supplied and could mix orgs), and
one bad ticket must not decide the fate of the other twenty-nine.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.v1.tickets as routes


@pytest.fixture
def tickets(monkeypatch):
    """uid -> repository_uid, with a stub TicketService.get_node."""
    table = {"t1": "repo-a", "t2": "repo-a", "t3": "repo-b"}

    async def _get_node(_self, uid):
        if uid not in table:
            raise HTTPException(status_code=404, detail=f"Ticket {uid} not found")
        return SimpleNamespace(uid=uid, repository_uid=table[uid])

    monkeypatch.setattr(routes.TicketService, "get_node", _get_node, raising=False)
    return table


@pytest.fixture
def only_repo_a(monkeypatch):
    """Tenancy that admits repo-a and 404s everything else."""
    seen = []

    async def _require(repository_uid, org_uid):
        seen.append(repository_uid)
        if repository_uid != "repo-a":
            raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(routes, "require_repo_in_org", _require)
    return seen


@pytest.mark.asyncio
async def test_every_uid_is_tenancy_checked(tickets, only_repo_a):
    async def _noop(_t):
        return None

    await routes._bulk(["t1", "t2"], "org-1", _noop)
    assert only_repo_a == ["repo-a", "repo-a"], "tenancy must run per ticket"


@pytest.mark.asyncio
async def test_a_foreign_ticket_is_rejected_without_stopping_the_rest(
    tickets, only_repo_a
):
    """The attack this guards: slipping another org's uid into the list."""
    acted = []

    async def _act(t):
        acted.append(t.uid)

    result = await routes._bulk(["t1", "t3", "t2"], "org-1", _act)

    assert acted == ["t1", "t2"], "the foreign ticket must never be acted on"
    assert result["ok"] == ["t1", "t2"]
    assert [e["uid"] for e in result["errors"]] == ["t3"]


@pytest.mark.asyncio
async def test_one_failure_does_not_abort_the_batch(tickets, only_repo_a):
    async def _act(t):
        if t.uid == "t1":
            raise HTTPException(status_code=409, detail="ticket is archived")

    result = await routes._bulk(["t1", "t2"], "org-1", _act)

    assert result["ok"] == ["t2"]
    assert result["errors"] == [{"uid": "t1", "detail": "ticket is archived"}]


@pytest.mark.asyncio
async def test_a_capacity_refusal_is_reported_not_raised(tickets, only_repo_a):
    """bulk-implement against a provider at its ceiling: the first few land and
    the rest are NAMED. Letting CapacityExceededError escape would turn a
    partially-successful batch into a 500."""
    from domains.runs.services.lifecycle import CapacityExceededError

    async def _act(t):
        if t.uid == "t2":
            raise CapacityExceededError("Claude is at its concurrency ceiling (5/5)")

    result = await routes._bulk(["t1", "t2"], "org-1", _act)

    assert result["ok"] == ["t1"]
    assert "ceiling" in result["errors"][0]["detail"]


@pytest.mark.asyncio
async def test_duplicate_uids_act_once(tickets, only_repo_a):
    acted = []

    async def _act(t):
        acted.append(t.uid)

    result = await routes._bulk(["t1", "t1", "t1"], "org-1", _act)
    assert acted == ["t1"]
    assert result["ok"] == ["t1"]


@pytest.mark.asyncio
async def test_a_missing_uid_is_an_error_entry_not_an_exception(tickets, only_repo_a):
    async def _noop(_t):
        return None

    result = await routes._bulk(["t1", "nope"], "org-1", _noop)
    assert result["ok"] == ["t1"]
    assert result["errors"][0]["uid"] == "nope"
