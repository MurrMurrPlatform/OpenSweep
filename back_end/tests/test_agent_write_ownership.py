"""Agents may only be edited IN PLACE by their owning org.

A shared agent (org_uid="" — every system/imported agent) or another org's
agent is visible for reference but must be customized via an org-scoped
override, never rewritten in place. Otherwise any tenant could tamper with a
globally-shared, write-capable agent's prompt. Operators (platform admins)
bypass — they curate the shared library. DB-free.
"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import domains.agents.services.agent_service as svc
from domains.agents.schemas import UpdateAgentRequest

pytestmark = pytest.mark.asyncio


class _Agent:
    def __init__(self, org_uid, provenance="user"):
        self.uid = "a1"
        self.org_uid = org_uid
        self.provenance = provenance
        self.prompt = "old"
        self.rev = 1
        self.tags = []
        self.produces = "findings"
        self.updated_at = datetime.now(UTC)

    async def save(self):
        return None


@pytest.fixture(autouse=True)
def wire(monkeypatch):
    monkeypatch.setattr(svc, "_snapshot_platform_revision", _anoop)
    monkeypatch.setattr(svc, "write_audit", _anoop)
    monkeypatch.setattr(svc, "to_dto", lambda a, **k: {"uid": a.uid, "prompt": a.prompt})


async def _anoop(*a, **k):
    return None


def _patch_agent(monkeypatch, agent):
    async def _get(uid, *, org_uid=""):
        return agent

    monkeypatch.setattr(svc, "get_agent_model", _get)


async def _update(org_uid, platform_admin):
    return await svc.update_agent(
        "a1",
        UpdateAgentRequest(prompt="new prompt"),
        org_uid=org_uid,
        actor_uid="u1",
        allow_write_produces=True,
        platform_admin=platform_admin,
    )


async def test_tenant_cannot_edit_shared_agent(monkeypatch):
    _patch_agent(monkeypatch, _Agent(org_uid="", provenance="imported"))
    with pytest.raises(HTTPException) as exc:
        await _update(org_uid="org-a", platform_admin=False)
    assert exc.value.status_code == 403


async def test_tenant_cannot_edit_other_orgs_agent(monkeypatch):
    _patch_agent(monkeypatch, _Agent(org_uid="org-b"))
    with pytest.raises(HTTPException) as exc:
        await _update(org_uid="org-a", platform_admin=False)
    assert exc.value.status_code == 403


async def test_owner_can_edit_own_agent(monkeypatch):
    _patch_agent(monkeypatch, _Agent(org_uid="org-a"))
    dto = await _update(org_uid="org-a", platform_admin=False)
    assert dto["prompt"] == "new prompt"


async def test_operator_can_edit_shared_agent(monkeypatch):
    _patch_agent(monkeypatch, _Agent(org_uid="", provenance="system"))
    dto = await _update(org_uid="org-a", platform_admin=True)
    assert dto["prompt"] == "new prompt"
