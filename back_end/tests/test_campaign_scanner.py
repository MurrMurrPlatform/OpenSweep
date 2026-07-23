"""Schedule scanner × run-campaign bindings — DB and campaign service
monkeypatched; a due binding plans AND launches (scheduled = pre-approved)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domains.agents.services import schedule_scanner
from domains.campaigns.services import campaign_service

NOW = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)  # a Monday


class _FakeSA(SimpleNamespace):
    async def save(self):
        self.saved = True


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def get_or_none(self, uid=None, **_kw):
        return next((r for r in self._rows if r.uid == uid), None)


def _binding(**overrides):
    fields = dict(
        uid="sa1",
        agent_uid="agent1",
        repository_uid="repo1",
        title="Weekly rotation campaign",
        trigger="cron:0 7 * * 1",
        target={"template": "rotation", "k": 2, "effort": "deep"},
        autonomy="ask-before-run",
        enabled=True,
        last_scheduled_at=None,
        saved=False,
    )
    fields.update(overrides)
    return _FakeSA(**fields)


@pytest.fixture
def seams(monkeypatch):
    captured = {}
    sa = _binding()
    agent = SimpleNamespace(uid="agent1", source_url="opensweep://agent/run-campaign")
    monkeypatch.setattr(
        schedule_scanner, "ScheduledAgent", SimpleNamespace(nodes=_Nodes([sa]))
    )
    monkeypatch.setattr(
        schedule_scanner, "Agent", SimpleNamespace(nodes=_Nodes([agent]))
    )

    async def fake_create(repository_uid, req, *, created_by="", trigger_provenance=""):
        captured["create"] = {
            "repository_uid": repository_uid,
            "req": req,
            "created_by": created_by,
            "trigger_provenance": trigger_provenance,
        }
        return SimpleNamespace(uid="campaign1")

    async def fake_launch(uid, **_kw):
        captured["launched"] = uid
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(campaign_service, "create", fake_create)
    monkeypatch.setattr(campaign_service, "launch", fake_launch)
    captured["sa"] = sa
    return captured


async def test_due_run_campaign_binding_creates_and_launches(seams):
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 1
    assert result.errors == []

    create = seams["create"]
    assert create["repository_uid"] == "repo1"
    assert create["req"].template == "rotation"
    assert create["req"].k == 2
    assert create["req"].effort == "deep"
    assert create["req"].area_prefix == ""  # not in the target → whole map
    assert create["created_by"] == "scheduled-agent:sa1"
    assert create["trigger_provenance"] == "cron:0 7 * * 1"
    assert seams["launched"] == "campaign1"
    # The binding was stamped so the next tick doesn't refire.
    assert seams["sa"].last_scheduled_at == NOW
    assert seams["sa"].saved is True


async def test_target_area_prefix_reaches_the_campaign_request(seams):
    seams["sa"].target = {"template": "full", "area_prefix": "backend"}
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 1
    assert seams["create"]["req"].template == "full"
    assert seams["create"]["req"].area_prefix == "backend"


async def test_disabled_autonomy_skips_but_stamps(seams):
    seams["sa"].autonomy = "disabled"
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert "create" not in seams and "launched" not in seams
    assert seams["sa"].last_scheduled_at == NOW  # kill-safety still consumes the tick


async def test_create_failure_is_an_error_not_a_crash(seams, monkeypatch):
    async def boom(*_a, **_kw):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(campaign_service, "create", boom)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert any("planner exploded" in e for e in result.errors)
    assert seams["sa"].last_scheduled_at is None  # not stamped — retried next tick


def test_seeded_weekly_rotation_campaign_binding_shape():
    import inspect

    from domains.agents.services.scheduled_agent_service import (
        ROTATION_CAMPAIGN_WEEKLY_TITLE,
        RUN_CAMPAIGN_KEY,
        seed_audit_agents,
    )

    assert RUN_CAMPAIGN_KEY == "run-campaign"
    assert ROTATION_CAMPAIGN_WEEKLY_TITLE == "Weekly rotation campaign"
    src = inspect.getsource(seed_audit_agents)
    # Seeded DISABLED on the Monday 07:00 cron with the kind-based rotation target.
    assert '"cron:0 7 * * 1"' in src
    assert '"kind": "subsystem"' in src
    assert '"selection": "rotation"' in src


async def test_kind_based_target_reaches_campaign_request(seams):
    """A binding target with kind/selection passes them through to the request."""
    seams["sa"].target = {"kind": "subsystem", "selection": "rotation", "k": 3}
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 1
    assert result.errors == []

    create = seams["create"]
    assert create["req"].kind == "subsystem"
    assert create["req"].selection == "rotation"
    assert create["req"].k == 3
    # No legacy template key in this new-style target.
    assert create["req"].template == "rotation"  # schema default, not from target


async def test_kind_based_seed_target_plans_and_launches(seams):
    """The seeded {"kind":"subsystem","selection":"rotation","k":3} target works
    end-to-end through the scanner: creates a campaign and launches it."""
    from domains.agents.services.scheduled_agent_service import (
        ROTATION_CAMPAIGN_WEEKLY_TITLE,
    )

    seams["sa"].target = {"kind": "subsystem", "selection": "rotation", "k": 3}
    seams["sa"].title = ROTATION_CAMPAIGN_WEEKLY_TITLE

    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 1
    assert seams["launched"] == "campaign1"
    assert seams["create"]["req"].kind == "subsystem"
    assert seams["create"]["req"].selection == "rotation"
