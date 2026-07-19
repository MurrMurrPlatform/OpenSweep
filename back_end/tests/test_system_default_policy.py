"""System-default RunPolicy upsert — legacy ceiling migration.

Pure-Python: the node is built in memory and save/find are monkeypatched,
so no Neo4j is needed.
"""

from domains.run_policies.models import RunPolicy
from domains.run_policies.services import system_default


def _node(**fields) -> RunPolicy:
    base = dict(uid="test-uid", name=system_default.SYSTEM_DEFAULT_NAME)
    base.update(fields)
    return RunPolicy(**base)


async def _run_upsert(monkeypatch, existing: RunPolicy) -> RunPolicy:
    async def find_by_name(name):
        return existing

    async def save(self):
        return self

    monkeypatch.setattr(system_default, "_find_by_name", find_by_name)
    monkeypatch.setattr(RunPolicy, "save", save)
    return await system_default.ensure_system_default()


async def test_legacy_seeded_dollars_migrate_forward(monkeypatch):
    # Both prior seeded caps ($1, then $3) were never human-tuned; the
    # upsert moves them to the current default.
    for legacy in (1.0, 3.0):
        p = await _run_upsert(monkeypatch, _node(max_dollars=legacy))
        assert p.max_dollars == system_default._DEFAULTS["max_dollars"]


async def test_human_tuned_dollars_preserved(monkeypatch):
    p = await _run_upsert(monkeypatch, _node(max_dollars=7.5))
    assert p.max_dollars == 7.5


async def test_current_default_is_unlimited(monkeypatch):
    assert system_default._DEFAULTS["max_dollars"] is None
    assert system_default._DEFAULTS["max_wall_seconds"] == 0
    assert system_default._DEFAULTS["max_tool_turns"] is None
