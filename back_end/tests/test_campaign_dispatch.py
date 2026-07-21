"""dispatch_part — compose/trigger seams monkeypatched, contracts asserted."""

from types import SimpleNamespace

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.services import part_dispatch
from domains.runs.schemas import RunTrigger


def _campaign(**overrides):
    fields = {
        "uid": "c1",
        "repository_uid": "repo1",
        "title": "Full audit campaign",
        "status": "running",
        "template": "full",
        "effort": "",
        "created_by": "user1",
        "trigger_provenance": "manual",
        "parts": [
            {
                "idx": 0,
                "kind": "area",
                "title": "Backend",
                "scope_paths": ["back_end/domains", "back_end/api"],
                "doc_uids": ["doc1"],
                "lens_keys": ["bugs", "security"],
                "run_uid": "",
                "state": "pending",
                "file_count": 80,
            },
            {
                "idx": 1,
                "kind": "global",
                "title": "Global sweep — architecture-review",
                "scope_paths": [],
                "doc_uids": [],
                "lens_keys": ["architecture-review"],
                "run_uid": "",
                "state": "pending",
                "file_count": None,
            },
        ],
    }
    fields.update(overrides)
    return Campaign(**fields)


def _lens(key, *, body="Check it.", global_agent_key=""):
    return SimpleNamespace(
        key=key, title=key.title(), body=body, global_agent_key=global_agent_key
    )


@pytest.fixture
def area_seams(monkeypatch):
    captured = {}

    async def fake_get_by_key(key):
        return _lens(key)

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(text="COMPOSED", agent_uid="agent1", agent_rev=2)

    async def fake_policy(tier):
        captured["tier"] = tier
        return SimpleNamespace(uid="policy1")

    async def fake_trigger_run(**kwargs):
        captured["trigger_run"] = kwargs
        return SimpleNamespace(uid="run1")

    monkeypatch.setattr(
        part_dispatch.lens_service, "get_by_key", fake_get_by_key
    )
    monkeypatch.setattr(part_dispatch, "compose_agent_intent", fake_compose)
    monkeypatch.setattr(part_dispatch, "ensure_policy_for_effort", fake_policy)
    monkeypatch.setattr(part_dispatch, "trigger_run", fake_trigger_run)
    return captured


async def test_area_part_structural_carries_the_three_contracts(area_seams):
    c = _campaign()
    run_uid = await part_dispatch.dispatch_part(c, c.parts[0])
    assert run_uid == "run1"

    structural = area_seams["compose"]["structural"]
    # Scope contract: part i of N, the paths, and the fence.
    assert "part 1 of 2 of audit campaign 'Full audit campaign'" in structural
    assert "- back_end/domains" in structural and "- back_end/api" in structural
    assert "Do not investigate outside this scope." in structural
    # Lens checklist (numbered sections) + escalation instruction.
    assert "## Audit lenses for this scope" in structural
    assert "### 1. Bugs" in structural and "### 2. Security" in structural
    assert "escalate:" in structural
    # Reporting contract.
    assert "covered_paths" in structural and "lens_verdicts" in structural
    assert area_seams["compose"]["agent_key"] == "ask"
    assert area_seams["compose"]["prompt_body"] is None


async def test_area_part_trigger_run_call_shape(area_seams):
    c = _campaign()
    await part_dispatch.dispatch_part(c, c.parts[0])
    kwargs = area_seams["trigger_run"]
    assert kwargs["intent"] == "COMPOSED"
    assert kwargs["playbook"] == "ask"
    assert kwargs["title"] == "Campaign: Backend"
    assert kwargs["run_policy_uid"] == "policy1"
    assert kwargs["effort"] == "normal"  # campaign.effort "" → children normal
    assert kwargs["trigger"] == RunTrigger.MANUAL
    # Target backlink: the tick + Checked stamps find their way home.
    target = kwargs["target"]
    assert target["campaign_uid"] == "c1"
    assert target["campaign_part"] == 0
    assert target["paths"] == ["back_end/domains", "back_end/api"]
    assert target["doc_uids"] == ["doc1"]
    # Docs-derived parts carry no area keys.
    assert target["area_keys"] == []


async def test_bundled_part_scope_contract_names_every_area(area_seams):
    c = _campaign()
    c.parts[0]["area_keys"] = ["backend/api", "backend/core"]
    await part_dispatch.dispatch_part(c, c.parts[0])
    structural = area_seams["compose"]["structural"]
    assert (
        "This part covers areas: backend/api, backend/core — audit all of them."
        in structural
    )
    assert area_seams["trigger_run"]["target"]["area_keys"] == [
        "backend/api",
        "backend/core",
    ]


async def test_single_area_part_gets_no_bundle_line(area_seams):
    c = _campaign()
    c.parts[0]["area_keys"] = ["backend"]
    await part_dispatch.dispatch_part(c, c.parts[0])
    assert "This part covers areas:" not in area_seams["compose"]["structural"]


async def test_cron_provenance_dispatches_as_schedule(area_seams):
    c = _campaign(trigger_provenance="cron:0 7 * * 1")
    await part_dispatch.dispatch_part(c, c.parts[0])
    assert area_seams["trigger_run"]["trigger"] == RunTrigger.SCHEDULE


async def test_explicit_campaign_effort_reaches_children(area_seams):
    c = _campaign(effort="deep")
    await part_dispatch.dispatch_part(c, c.parts[0])
    assert area_seams["trigger_run"]["effort"] == "deep"


@pytest.fixture
def global_seams(monkeypatch):
    captured = {}
    variant = SimpleNamespace(uid="variant1", title="Architecture review")

    async def fake_get_by_key(key):
        return _lens(key, global_agent_key="architecture-review")

    async def fake_by_url(url):
        captured["variant_url"] = url
        return variant

    async def fake_digest(repository_uid, lens_key):
        captured["digest_args"] = (repository_uid, lens_key)
        return ["- Leaky boundary (back_end/app.py)"]

    async def fake_dispatch_agent(**kwargs):
        captured["dispatch_agent"] = kwargs
        return SimpleNamespace(uid="run2")

    monkeypatch.setattr(part_dispatch.lens_service, "get_by_key", fake_get_by_key)
    monkeypatch.setattr(part_dispatch, "system_agent_by_url", fake_by_url)
    monkeypatch.setattr(part_dispatch, "_escalation_digest", fake_digest)
    monkeypatch.setattr(part_dispatch, "dispatch_agent", fake_dispatch_agent)
    captured["variant"] = variant
    return captured


async def test_global_part_dispatches_the_seeded_variant(global_seams):
    c = _campaign()
    run_uid = await part_dispatch.dispatch_part(c, c.parts[1])
    assert run_uid == "run2"
    assert global_seams["variant_url"] == "opensweep://library/architecture-review"
    kwargs = global_seams["dispatch_agent"]
    assert kwargs["agent"] is global_seams["variant"]
    # Global sweeps default deep when the campaign pinned no tier.
    assert kwargs["effort"] == "deep"
    assert kwargs["target"]["campaign_uid"] == "c1"
    assert kwargs["target"]["campaign_part"] == 1
    assert kwargs["target"]["escalations"] == ["- Leaky boundary (back_end/app.py)"]
    assert "Leaky boundary" in kwargs["structural_extra"]
    assert global_seams["digest_args"] == ("repo1", "architecture-review")


async def test_global_part_missing_variant_raises(global_seams, monkeypatch):
    async def gone(url):
        return None

    monkeypatch.setattr(part_dispatch, "system_agent_by_url", gone)
    with pytest.raises(Exception, match="no seeded variant"):
        await part_dispatch.dispatch_part(_campaign(), _campaign().parts[1])
