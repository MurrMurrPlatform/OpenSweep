"""Feature part dispatch — the Area's spec is loaded fresh and inlined
between the lens checklist and the reporting contract; a missing/disabled/
spec-less area degrades to a plain area dispatch (part states are sticky —
a raise would fail the part forever) with a visible degradation note in
the structural and a campaign.feature_part_degraded audit event."""

from types import SimpleNamespace

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.services import part_dispatch

_SPEC_HEADING = (
    "## Feature spec — verify the implementation matches this contract end-to-end"
)


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
                "kind": "feature",
                "title": "Checkout",
                "scope_paths": ["be/checkout"],
                "doc_uids": ["doc1"],
                "lens_keys": ["implementation-gaps"],
                "run_uid": "",
                "state": "pending",
                "file_count": 12,
                "area_key": "features/checkout",
            },
        ],
    }
    fields.update(overrides)
    return Campaign(**fields)


@pytest.fixture
def seams(monkeypatch):
    captured = {}

    async def fake_get_by_key(key):
        return SimpleNamespace(
            key=key, title=key.title(), body="Find the gaps.", global_agent_key=""
        )

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(text="COMPOSED", agent_uid="agent1", agent_rev=2)

    async def fake_policy(tier):
        return SimpleNamespace(uid="policy1")

    async def fake_trigger_run(**kwargs):
        captured["trigger_run"] = kwargs
        return SimpleNamespace(uid="run1")

    monkeypatch.setattr(part_dispatch.lens_service, "get_by_key", fake_get_by_key)
    monkeypatch.setattr(part_dispatch, "compose_agent_intent", fake_compose)
    monkeypatch.setattr(part_dispatch, "ensure_policy_for_effort", fake_policy)
    monkeypatch.setattr(part_dispatch, "trigger_run", fake_trigger_run)
    return captured


def _area_stub(monkeypatch, area):
    captured = {}

    async def fake_get_area_by_key(repository_uid, key):
        captured["args"] = (repository_uid, key)
        return area

    monkeypatch.setattr(
        part_dispatch.area_service, "get_area_by_key", fake_get_area_by_key
    )
    return captured


async def test_feature_part_structural_inlines_the_spec(seams, monkeypatch):
    lookup = _area_stub(
        monkeypatch,
        SimpleNamespace(enabled=True, spec="The checkout MUST retry payment once."),
    )
    c = _campaign()
    run_uid = await part_dispatch.dispatch_part(c, c.parts[0])
    assert run_uid == "run1"
    # The area is loaded fresh at dispatch time, by (repository_uid, key).
    assert lookup["args"] == ("repo1", "features/checkout")

    structural = seams["compose"]["structural"]
    # Scope contract + checklist + SPEC + reporting contract, in that order.
    assert "Do not investigate outside this scope." in structural
    assert "## Audit lenses for this scope" in structural
    assert _SPEC_HEADING in structural
    assert "The checkout MUST retry payment once." in structural
    assert "lens_verdicts" in structural
    assert structural.index("## Audit lenses") < structural.index(_SPEC_HEADING)
    assert structural.index(_SPEC_HEADING) < structural.index("covered_paths")
    # Feature parts run the same seeded "ask" base as area parts.
    assert seams["compose"]["agent_key"] == "ask"
    # The target backlink carries the part's area key.
    assert seams["trigger_run"]["target"]["area_key"] == "features/checkout"
    assert seams["trigger_run"]["title"] == "Campaign: Checkout"


@pytest.mark.parametrize(
    ("area", "reason"),
    [
        (None, "not found"),  # deleted since planning
        (SimpleNamespace(enabled=False, spec="stale"), "disabled"),  # disabled
        (SimpleNamespace(enabled=True, spec="   "), "has no spec"),  # never written
    ],
)
async def test_degraded_feature_part_dispatches_as_plain_area_no_raise(
    seams, monkeypatch, area, reason
):
    _area_stub(monkeypatch, area)
    audits = []

    async def fake_write_audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(part_dispatch, "write_audit", fake_write_audit)
    c = _campaign()
    run_uid = await part_dispatch.dispatch_part(c, c.parts[0])
    assert run_uid == "run1"  # dispatched anyway — never raises
    structural = seams["compose"]["structural"]
    assert _SPEC_HEADING not in structural
    # The degradation is visible to the agent: a note replaces the spec block.
    assert "## Note — feature spec unavailable" in structural
    assert (
        "planned as a feature-spec audit of area 'features/checkout', "
        f"but that area {reason}." in structural
    )
    assert "the spec contract could NOT be verified — say so in your report." in structural
    # Still a full area contract: scope + checklist + reporting.
    assert "Do not investigate outside this scope." in structural
    assert "## Audit lenses for this scope" in structural
    assert "lens_verdicts" in structural
    # …and to operators: one campaign.feature_part_degraded audit event.
    (audit,) = audits
    assert audit["kind"] == "campaign.feature_part_degraded"
    assert audit["subject_uid"] == "c1" and audit["subject_type"] == "Campaign"
    assert audit["repository_uid"] == "repo1"
    assert audit["payload"] == {
        "part": 0,
        "area_key": "features/checkout",
        "reason": reason,
    }
