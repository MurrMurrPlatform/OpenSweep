"""`disabled` must stop a due cron tick on EVERY branch of the scanner.

Regression: the three anchor branches (audit-stale, run-campaign, map-areas)
each checked `autonomy == "disabled"`, but the generic fallthrough — the path
the seeded deep-issue-hunt and security-audit bindings take, both of which
seed `enabled=True` — dispatched unconditionally. Setting a binding to
`disabled`, the one level that means "never run this", still auto-billed a
run every time its cron came due.
"""

from datetime import UTC, datetime, timedelta

import pytest

from domains.agents.services import schedule_scanner
from domains.agents.services.schedule_scanner import cron_dispatch_allowed


class _SA:
    """Minimal ScheduledAgent stand-in; `save` records the tick stamp."""

    def __init__(self, *, autonomy: str, uid: str = "sa-1") -> None:
        self.uid = uid
        self.agent_uid = "agent-1"
        self.repository_uid = "repo-1"
        self.trigger = "cron:0 6 * * *"
        self.target = {}
        self.autonomy = autonomy
        self.enabled = True
        self.effort = ""
        self.run_policy_uid = ""
        self.title = "Weekly deep issue hunt"
        # Long past, so the cron is unambiguously due.
        self.last_scheduled_at = datetime.now(UTC) - timedelta(days=30)
        self.saved = 0

    async def save(self):
        self.saved += 1
        return self


class _Agent:
    """A generic agent — no anchor key, so it takes the fallthrough branch."""

    uid = "agent-1"
    source_url = "https://example.test/agents/deep-issue-hunt"
    enabled = True


def _install(monkeypatch, sa):
    class _Nodes:
        async def all(self):
            return [sa]

    class _AgentNodes:
        async def get_or_none(self, uid=None):
            return _Agent()

    monkeypatch.setattr(schedule_scanner.ScheduledAgent, "nodes", _Nodes())
    monkeypatch.setattr(schedule_scanner.Agent, "nodes", _AgentNodes())
    # The fallthrough resolves a generic key, not an anchor.
    monkeypatch.setattr(schedule_scanner, "agent_key", lambda url: "deep-issue-hunt")

    dispatched: list[str] = []

    async def _fake_trigger(uid, **kwargs):
        dispatched.append(uid)
        return object()

    monkeypatch.setattr(
        "domains.agents.services.dispatch.trigger_scheduled_agent", _fake_trigger
    )
    return dispatched


@pytest.mark.parametrize(
    "autonomy",
    ["ask-before-run", "suggest", "auto-run-cheap", "auto-run-any"],
)
async def test_non_disabled_levels_still_dispatch_on_cron(monkeypatch, autonomy):
    """Cron IS the approval — tightening this would silently stop every
    existing binding, since `ask-before-run` is the model default."""
    sa = _SA(autonomy=autonomy)
    dispatched = _install(monkeypatch, sa)

    result = await schedule_scanner.scan_and_dispatch()

    assert dispatched == [sa.uid], autonomy
    assert result.dispatched == 1


async def test_disabled_autonomy_blocks_the_generic_fallthrough(monkeypatch):
    """The headline regression: `disabled` used to dispatch anyway here."""
    sa = _SA(autonomy="disabled")
    dispatched = _install(monkeypatch, sa)

    result = await schedule_scanner.scan_and_dispatch()

    assert dispatched == []
    assert result.dispatched == 0


async def test_disabled_tick_is_stamped_so_it_does_not_re_evaluate(monkeypatch):
    """Without the stamp a disabled binding re-checks on every beat."""
    sa = _SA(autonomy="disabled")
    _install(monkeypatch, sa)

    await schedule_scanner.scan_and_dispatch()

    assert sa.saved == 1


def test_predicate_matches_the_anchor_branches():
    assert cron_dispatch_allowed("disabled") is False
    for level in ("", "suggest", "ask-before-run", "auto-run-cheap", "auto-run-any"):
        assert cron_dispatch_allowed(level) is True, level
