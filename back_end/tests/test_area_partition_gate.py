"""The area-map partition gate: a mapping run may not finish on a half-map.

Production failure this pins: a Map-areas run proposed ~24 subsystem/ignore
areas (a complete subsystem partition) plus 5 feature areas covering one
directory, emitted complete_run, and stopped after ONE turn — nothing looked
for the gap. The gate makes an incomplete axis force the continuation pass
that `envelope_has_complete_run` would otherwise suppress.

DB, git and the CLI are faked (the adapter's lazy imports are patched at their
module, test_area_service.py style); the assertions are about what the run
DOES, not which helper it called.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domains.executors import cli_tracking
from domains.executors.base import DispatchRequest

pytestmark = pytest.mark.asyncio


class _Nodes:
    def __init__(self, rows):
        self._rows = list(rows)

    async def all(self):
        return list(self._rows)

    async def filter(self, **kw):
        return [
            r for r in self._rows if all(getattr(r, k, None) == v for k, v in kw.items())
        ]

    async def get_or_none(self, **kw):
        return next(
            (r for r in self._rows if all(getattr(r, k, None) == v for k, v in kw.items())),
            None,
        )


def _area(key, kind="subsystem", scope_paths=(), enabled=True):
    return SimpleNamespace(
        uid=key, repository_uid="repo1", key=key, kind=kind,
        scope_paths=list(scope_paths), enabled=enabled,
    )


def _edit(key, kind="subsystem", scope_paths=(), run_uid="r1"):
    return SimpleNamespace(
        uid=f"e-{key}", repository_uid="repo1", key=key, kind=kind,
        scope_paths=list(scope_paths), proposed_enabled=True, status="pending",
        source_run_uid=run_uid,
    )


def _req(**overrides):
    base = dict(
        run_uid="r1",
        scheduled_agent_uid="",
        repository_uid="repo1",
        repository_local_path="/ws",
        intent="map the areas",
    )
    base.update(overrides)
    return DispatchRequest(**base)


def _propose(key, kind="subsystem", scope_paths=()):
    return {"tool": "propose_area_edit",
            "args": {"key": key, "kind": kind, "scope_paths": list(scope_paths)}}


_COMPLETE = {"tool": "complete_run", "args": {"summary": "mapped the repo"}}


def _wire(monkeypatch, *, tracked, areas=(), edits=(), agent_key="map-areas"):
    """Fake the three things the gate reaches for: git, the graph, the agent."""
    async def _git(path, *args, **kw):
        return "\n".join(tracked) + "\n"

    monkeypatch.setattr("domains.runs.services.run_changes._git", _git)
    monkeypatch.setattr(
        "domains.areas.models.Area", SimpleNamespace(nodes=_Nodes(areas))
    )
    monkeypatch.setattr(
        "domains.areas.models.AreaEdit", SimpleNamespace(nodes=_Nodes(edits))
    )

    async def _key(req):
        return agent_key

    monkeypatch.setattr(cli_tracking, "_run_agent_key", _key)


# ── reading the run's own proposals out of the envelope ─────────────────────


async def test_envelope_area_proposals_picks_out_the_area_calls():
    envelope = {"tool_calls": [
        _propose("backend", scope_paths=["back_end"]),
        {"tool": "create_finding", "args": {}},
        _COMPLETE,
    ]}
    assert cli_tracking.envelope_area_proposals(envelope) == [
        {"key": "backend", "kind": "subsystem", "scope_paths": ["back_end"]}
    ]


async def test_envelope_area_proposals_empty_for_no_envelope():
    assert cli_tracking.envelope_area_proposals(None) == []
    assert cli_tracking.envelope_area_proposals({"tool_calls": []}) == []


# ── when the gate speaks up ─────────────────────────────────────────────────


async def test_gap_nudge_names_the_incomplete_axis_and_its_paths(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py", "front_end/main.ts", "uv.lock"])
    envelope = {"tool_calls": [
        _propose("backend", scope_paths=["back_end"]),
        _propose("frontend", scope_paths=["front_end"]),
        _propose("locks", kind="ignore", scope_paths=["uv.lock"]),
        _COMPLETE,
    ]}

    nudge = await cli_tracking.area_partition_nudge(_req(), envelope)

    assert "feature axis incomplete" in nudge
    assert "back_end/app.py" in nudge
    assert "feature_ignore" in nudge  # tells it HOW to close the axis
    assert "subsystem axis incomplete" not in nudge  # that axis is whole


async def test_no_nudge_when_both_axes_partition_the_tree(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py", "uv.lock"])
    envelope = {"tool_calls": [
        _propose("backend", scope_paths=["back_end"]),
        _propose("locks", kind="ignore", scope_paths=["uv.lock"]),
        _propose("features/checkout", kind="feature", scope_paths=["back_end"]),
        _propose("features/none", kind="feature_ignore", scope_paths=["uv.lock"]),
        _COMPLETE,
    ]}

    assert await cli_tracking.area_partition_nudge(_req(), envelope) == ""


async def test_accepted_areas_and_pending_edits_count_as_coverage(monkeypatch):
    """A map-areas run on the MCP transport files its proposals live, so the
    envelope holds nothing but complete_run — the map still has to be read from
    the graph or every such run would look like a total gap."""
    _wire(
        monkeypatch,
        tracked=["back_end/app.py", "uv.lock"],
        areas=[
            _area("backend", scope_paths=["back_end"]),
            _area("locks", kind="ignore", scope_paths=["uv.lock"]),
        ],
        edits=[
            _edit("features/checkout", kind="feature", scope_paths=["back_end"]),
            _edit("features/none", kind="feature_ignore", scope_paths=["uv.lock"]),
        ],
    )

    assert await cli_tracking.area_partition_nudge(_req(), {"tool_calls": [_COMPLETE]}) == ""


async def test_a_run_reproposing_an_area_replaces_its_accepted_scope(monkeypatch):
    """The proposal is the area's NEXT shape: a narrowed scope must show up as
    the gap it creates, not be masked by the accepted row it replaces."""
    _wire(
        monkeypatch,
        tracked=["back_end/a.py", "back_end/b.py"],
        areas=[_area("backend", scope_paths=["back_end"])],
        agent_key="map-areas",
    )
    envelope = {"tool_calls": [_propose("backend", scope_paths=["back_end/a.py"]), _COMPLETE]}

    nudge = await cli_tracking.area_partition_nudge(_req(), envelope)

    assert "subsystem axis incomplete" in nudge and "back_end/b.py" in nudge


# ── when the gate stays out of the way ──────────────────────────────────────


async def test_an_incidental_area_fix_is_not_held_to_a_whole_repo_partition(monkeypatch):
    """One or two proposals from a non-mapping run is a touch-up, not a remap —
    forcing it to partition the repository would hijack the run."""
    _wire(monkeypatch, tracked=["back_end/app.py"], agent_key="ask")
    envelope = {"tool_calls": [_propose("backend", scope_paths=["back_end"]), _COMPLETE]}

    assert await cli_tracking.area_partition_nudge(_req(), envelope) == ""


async def test_a_run_that_proposed_nothing_is_never_a_mapping_run(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py"], agent_key="ask")

    assert await cli_tracking.area_partition_nudge(_req(), {"tool_calls": [_COMPLETE]}) == ""


async def test_many_proposals_make_a_mapping_run_whatever_it_was_dispatched_as(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py", "front_end/main.ts"], agent_key="ask")
    envelope = {"tool_calls": [
        _propose("backend", scope_paths=["back_end"]),
        _propose("frontend", scope_paths=["front_end"]),
        _propose("docs", scope_paths=["docs"]),
        _COMPLETE,
    ]}

    assert "feature axis incomplete" in await cli_tracking.area_partition_nudge(_req(), envelope)


# ── degrade, never break the run ────────────────────────────────────────────


async def test_no_workspace_clone_skips_the_gate(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py"])
    envelope = {"tool_calls": [_propose("a"), _propose("b"), _propose("c")]}

    assert await cli_tracking.area_partition_nudge(
        _req(repository_local_path=""), envelope
    ) == ""


async def test_a_failing_git_skips_the_gate(monkeypatch):
    _wire(monkeypatch, tracked=[])

    async def _boom(path, *args, **kw):
        raise RuntimeError("git ls-files failed: not a repository")

    monkeypatch.setattr("domains.runs.services.run_changes._git", _boom)
    envelope = {"tool_calls": [_propose("a"), _propose("b"), _propose("c")]}

    assert await cli_tracking.area_partition_nudge(_req(), envelope) == ""


async def test_an_empty_tree_skips_the_gate(monkeypatch):
    """No tracked files means "unknown", not "nothing is covered"."""
    _wire(monkeypatch, tracked=[])
    envelope = {"tool_calls": [_propose("a"), _propose("b"), _propose("c")]}

    assert await cli_tracking.area_partition_nudge(_req(), envelope) == ""


async def test_an_unreachable_graph_skips_the_gate(monkeypatch):
    _wire(monkeypatch, tracked=["back_end/app.py"])

    class _Down:
        async def filter(self, **kw):
            raise RuntimeError("neo4j unavailable")

    monkeypatch.setattr("domains.areas.models.Area", SimpleNamespace(nodes=_Down()))
    envelope = {"tool_calls": [_propose("a"), _propose("b"), _propose("c")]}

    assert await cli_tracking.area_partition_nudge(_req(), envelope) == ""


# ── which runs count as mapping runs ────────────────────────────────────────


async def test_run_agent_key_reads_the_scheduled_agents_base(monkeypatch):
    monkeypatch.setattr(
        "domains.agents.models.ScheduledAgent",
        SimpleNamespace(nodes=_Nodes([SimpleNamespace(uid="sa1", agent_uid="ag1")])),
    )
    monkeypatch.setattr(
        "domains.agents.models.Agent",
        SimpleNamespace(nodes=_Nodes([
            SimpleNamespace(uid="ag1", source_url="opensweep://agent/map-areas")
        ])),
    )
    monkeypatch.setattr("domains.runs.models.Run", SimpleNamespace(nodes=_Nodes([])))

    assert await cli_tracking._run_agent_key(_req(scheduled_agent_uid="sa1")) == "map-areas"


async def test_run_agent_key_falls_back_to_the_runs_composed_agent(monkeypatch):
    """Map-areas runs are dispatched by the sweep flow with NO ScheduledAgent —
    reading only the binding would miss every one of them."""
    monkeypatch.setattr("domains.agents.models.ScheduledAgent", SimpleNamespace(nodes=_Nodes([])))
    monkeypatch.setattr(
        "domains.runs.models.Run",
        SimpleNamespace(nodes=_Nodes([SimpleNamespace(uid="r1", agent_uid="ag1")])),
    )
    monkeypatch.setattr(
        "domains.agents.models.Agent",
        SimpleNamespace(nodes=_Nodes([
            SimpleNamespace(uid="ag1", source_url="opensweep://agent/map-areas")
        ])),
    )

    assert await cli_tracking._run_agent_key(_req()) == "map-areas"


async def test_run_agent_key_is_empty_without_an_agent(monkeypatch):
    monkeypatch.setattr("domains.agents.models.ScheduledAgent", SimpleNamespace(nodes=_Nodes([])))
    monkeypatch.setattr("domains.runs.models.Run", SimpleNamespace(nodes=_Nodes([])))
    monkeypatch.setattr("domains.agents.models.Agent", SimpleNamespace(nodes=_Nodes([])))

    assert await cli_tracking._run_agent_key(_req()) == ""


# ── end to end through the adapter: does the run actually continue? ─────────


def _envelope_text(calls, summary="mapped the repo"):
    return (
        "Here is the map.\n\n```json\n"
        + json.dumps({"tool_calls": calls, "summary": summary})
        + "\n```"
    )


async def _dispatch(monkeypatch, *, calls, tracked, app_server=False, agent_key="map-areas"):
    """One full codex dispatch; returns (result, instructions each pass got)."""
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="",
                               credential_revision=0, extra_args="", org_uid="org-a")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: app_server)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _none())
    monkeypatch.setattr(cli_tracking.artifact_store, "put", lambda **kw: "artifact://raw")

    @asynccontextmanager
    async def _txn(_p):
        yield

    monkeypatch.setattr(cli_tracking.codex_credential, "codex_credential_txn", _txn)

    async def _exec_calls(*, calls, req, executor_value, deny_tools):
        return ([], [], {})

    monkeypatch.setattr(cli_tracking, "execute_envelope_tool_calls", _exec_calls)

    async def _completed(uid):
        return False  # the envelope's complete_run is the only completion signal

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _completed)
    _wire(monkeypatch, tracked=tracked, agent_key=agent_key)

    prompts: list[str] = []
    first = _envelope_text(calls)
    # The continuation answers with a complete map so the pass can settle.
    second = _envelope_text([_COMPLETE], summary="finished the partition")

    def _next(instruction):
        prompts.append(instruction)
        return first if len(prompts) == 1 else second

    from domains.llm_providers.services.llm_executor import LLMInvocation

    if app_server:
        async def fake_app_server(session, prov, *, system_prompt, instruction,
                                  timeout_seconds=None, working_dir=None,
                                  on_chunk=None, run_uid=""):
            return LLMInvocation(raw_output=_next(instruction), exit_code=0,
                                 transport="app-server")

        session = SimpleNamespace(uid="p1", client=None)

        async def _acquire(p):
            return session

        async def _release(s):
            return None

        monkeypatch.setattr(cli_tracking.codex_cli, "acquire_app_server", _acquire)
        monkeypatch.setattr(cli_tracking.codex_cli, "invoke_via_app_server", fake_app_server)
        monkeypatch.setattr(cli_tracking.codex_cli, "release_app_server", _release)
    else:
        async def fake_cli(prov, **kw):
            return LLMInvocation(raw_output=_next(kw["instruction"]), exit_code=0,
                                 transport="cli")

        monkeypatch.setattr(cli_tracking, "invoke_provider", fake_cli)

    result = await cli_tracking.CodexAdapter().dispatch(_req())
    return result, prompts


async def _none():
    return None


# The production shape: the subsystem axis tiles the tree, the feature axis
# covers one directory, and the run signs off with complete_run.
_HALF_MAP = [
    _propose("backend", scope_paths=["back_end"]),
    _propose("frontend", scope_paths=["front_end"]),
    _propose("locks", kind="ignore", scope_paths=["uv.lock"]),
    _propose("features/areas", kind="feature", scope_paths=["back_end/domains/areas"]),
    _COMPLETE,
]
_HALF_MAP_TREE = [
    "back_end/app.py",
    "back_end/domains/areas/svc.py",
    "front_end/main.ts",
    "uv.lock",
]

_WHOLE_MAP = [
    _propose("backend", scope_paths=["back_end"]),
    _propose("frontend", scope_paths=["front_end"]),
    _propose("locks", kind="ignore", scope_paths=["uv.lock"]),
    _propose("features/areas", kind="feature", scope_paths=["back_end", "front_end"]),
    _propose("features/none", kind="feature_ignore", scope_paths=["uv.lock"]),
    _COMPLETE,
]


async def test_half_mapped_run_is_sent_back_despite_complete_run(monkeypatch):
    result, prompts = await _dispatch(monkeypatch, calls=_HALF_MAP, tracked=_HALF_MAP_TREE)

    assert len(prompts) == 2, "complete_run must not end a run that left an axis open"
    assert "feature axis incomplete" in prompts[1]
    assert "front_end/main.ts" in prompts[1]  # the specific orphans, not a scolding
    assert result.usage["continuation_pass"] is True
    assert result.usage["continuation_reason"] == "area_partition_gap"


async def test_a_finished_map_is_left_alone(monkeypatch):
    result, prompts = await _dispatch(monkeypatch, calls=_WHOLE_MAP, tracked=_HALF_MAP_TREE)

    assert len(prompts) == 1, "a complete map must not be re-prompted"
    assert result.usage["continuation_pass"] is False


async def test_both_transports_enforce_the_partition_identically(monkeypatch):
    exec_result, exec_prompts = await _dispatch(
        monkeypatch, calls=_HALF_MAP, tracked=_HALF_MAP_TREE, app_server=False
    )
    app_result, app_prompts = await _dispatch(
        monkeypatch, calls=_HALF_MAP, tracked=_HALF_MAP_TREE, app_server=True
    )

    assert len(exec_prompts) == len(app_prompts) == 2
    assert exec_prompts[1] == app_prompts[1]
    assert exec_result.usage["continuation_reason"] == app_result.usage["continuation_reason"]


async def test_the_policy_continuation_ban_still_wins(monkeypatch):
    """max_continuation_passes=0 is an operator decision — an incomplete axis is
    reported, never retried against policy."""
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="",
                               credential_revision=0, extra_args="", org_uid="org-a")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: False)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _none())
    monkeypatch.setattr(cli_tracking.artifact_store, "put", lambda **kw: "artifact://raw")

    @asynccontextmanager
    async def _txn(_p):
        yield

    monkeypatch.setattr(cli_tracking.codex_credential, "codex_credential_txn", _txn)

    async def _exec_calls(*, calls, req, executor_value, deny_tools):
        return ([], [], {})

    monkeypatch.setattr(cli_tracking, "execute_envelope_tool_calls", _exec_calls)

    async def _completed(uid):
        return False

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _completed)
    _wire(monkeypatch, tracked=_HALF_MAP_TREE)

    from domains.llm_providers.services.llm_executor import LLMInvocation

    prompts: list[str] = []

    async def fake_cli(prov, **kw):
        prompts.append(kw["instruction"])
        return LLMInvocation(raw_output=_envelope_text(_HALF_MAP), exit_code=0, transport="cli")

    monkeypatch.setattr(cli_tracking, "invoke_provider", fake_cli)

    req = _req()
    req.policy = SimpleNamespace(
        max_wall_seconds=3600, max_tool_turns=200, max_files_touched=100,
        max_continuation_passes=0, warn_at_pct=80,
    )
    result = await cli_tracking.CodexAdapter().dispatch(req)

    assert len(prompts) == 1
    assert result.usage["continuation_pass"] is False
