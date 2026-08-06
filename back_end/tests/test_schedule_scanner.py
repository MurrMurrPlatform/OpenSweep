"""Pure tests for the cron schedule scanner's due-time logic, plus the
audit-stale branch's stamp-on-failure behavior (a failing repo must consume
its tick like every sibling branch, or it re-fires every beat)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domains.agents.services import schedule_scanner
from domains.agents.services.schedule_scanner import is_due
from domains.runs.services import sweep

NOW = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)  # a Monday


class _FakeSA(SimpleNamespace):
    async def save(self):
        self.saved = True


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def filter(self, **kw):
        def _match(row):
            for k, v in kw.items():
                if getattr(row, k, None) != v:
                    return False
            return True

        return [r for r in self._rows if _match(r)]

    async def get_or_none(self, uid=None, **_kw):
        return next((r for r in self._rows if r.uid == uid), None)


def _audit_binding(**overrides):
    fields = dict(
        uid="sa-audit",
        agent_uid="agent-audit",
        repository_uid="repo1",
        title="Auto-audit stale pages",
        trigger="cron:0 9 * * 1",
        target={"limit": 3},
        autonomy="ask-before-run",
        enabled=True,
        last_scheduled_at=None,
        saved=False,
    )
    fields.update(overrides)
    return _FakeSA(**fields)


@pytest.fixture
def audit_seams(monkeypatch):
    sa = _audit_binding()
    agent = SimpleNamespace(uid="agent-audit", source_url="opensweep://agent/audit-stale")
    monkeypatch.setattr(
        schedule_scanner, "ScheduledAgent", SimpleNamespace(nodes=_Nodes([sa]))
    )
    monkeypatch.setattr(
        schedule_scanner, "Agent", SimpleNamespace(nodes=_Nodes([agent]))
    )
    return sa


@pytest.mark.asyncio
async def test_audit_stale_success_stamps_and_dispatches(audit_seams, monkeypatch):
    async def fake_audit(*, repository_uid, limit, triggered_by):
        return SimpleNamespace(runs_dispatched=["run-a", "run-b"])

    monkeypatch.setattr(sweep, "run_auto_audit", fake_audit)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 2
    assert result.errors == []
    assert audit_seams.last_scheduled_at == NOW
    assert audit_seams.saved is True


@pytest.mark.asyncio
async def test_audit_stale_failure_still_stamps_the_tick(audit_seams, monkeypatch):
    async def boom(*, repository_uid, limit, triggered_by):
        raise RuntimeError("auto-audit exploded")

    monkeypatch.setattr(sweep, "run_auto_audit", boom)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert any("auto-audit exploded" in e for e in result.errors)
    # The failing branch must consume its tick — matching every sibling — so a
    # broken repo doesn't re-fire the binding every single beat.
    assert audit_seams.last_scheduled_at == NOW
    assert audit_seams.saved is True


def test_is_due_fires_when_previous_cron_tick_is_after_last_run():
    # 09:00 every weekday. We're at 09:30 Monday, last run was Friday.
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)  # Monday
    last = datetime(2026, 5, 22, 9, 5, tzinfo=timezone.utc)  # last Friday's fire
    assert is_due("0 9 * * 1-5", last=last, now=now) is True


def test_is_due_skips_when_already_dispatched_this_minute():
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)
    last = datetime(2026, 5, 25, 9, 10, tzinfo=timezone.utc)  # already after the 09:00 fire
    assert is_due("0 9 * * *", last=last, now=now) is False


def test_is_due_first_run_fires_immediately():
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)
    assert is_due("0 9 * * *", last=None, now=now) is True


def test_is_due_rejects_invalid_expression():
    with pytest.raises(ValueError):
        is_due("not a cron", last=None, now=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_scan_pushes_enabled_filter_into_query(monkeypatch):
    """Regression: `.nodes.all()` scanned the whole platform's bindings on
    every 60s beat. The scanner must query with `enabled=True` so disabled
    rows never even leave Cypher."""
    sa_enabled = _audit_binding(uid="sa-on", enabled=True)
    sa_disabled = _audit_binding(uid="sa-off", enabled=False)
    agent = SimpleNamespace(uid="agent-audit", source_url="opensweep://agent/audit-stale")
    monkeypatch.setattr(
        schedule_scanner,
        "ScheduledAgent",
        SimpleNamespace(nodes=_Nodes([sa_enabled, sa_disabled])),
    )
    monkeypatch.setattr(
        schedule_scanner, "Agent", SimpleNamespace(nodes=_Nodes([agent]))
    )

    async def fake_audit(*, repository_uid, limit, triggered_by):
        return SimpleNamespace(runs_dispatched=["r"])

    monkeypatch.setattr(sweep, "run_auto_audit", fake_audit)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    # The disabled row must never even be considered.
    assert sa_disabled.saved is False
    assert sa_enabled.saved is True
    assert result.dispatched == 1
