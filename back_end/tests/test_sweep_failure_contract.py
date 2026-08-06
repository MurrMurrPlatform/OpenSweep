"""The sweep entry points' real failure contract: 200-with-errors, not 409.

The docstrings used to promise "the gate raises LifecycleError — the API
converts it to a 409" without saying that this covers ONLY the pre-dispatch
gate. Every sweep function wraps its own `trigger_run` call in a
`try/except LifecycleError` + `except Exception`, appends the message to
`result.errors` and returns normally, so every DISPATCH-time failure (policy
violation, repository-not-found, sandbox prep) comes back as HTTP 200 with an
empty `run_uid`. A client that trusts the status code alone reports a failed
sweep as a success.

These tests pin both halves of the contract so it can't silently drift, and
assert the module docstring keeps saying so.
"""

from types import SimpleNamespace

import pytest

from domains.runs.services import sweep
from domains.runs.services.lifecycle import LifecycleError


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def filter(self, **kwargs):
        return [
            r for r in self._rows
            if all(getattr(r, k, None) == v for k, v in kwargs.items())
        ]


def _subsystem_area():
    return SimpleNamespace(
        repository_uid="r1",
        enabled=True,
        kind="subsystem",
        key="backend",
        title="Backend",
        scope_paths=["back_end"],
    )


@pytest.fixture
def seams(monkeypatch):
    async def _none(*_a, **_kw):
        return None

    async def _pages(_repository_uid):
        return "(none yet)"

    async def _areas(_repository_uid):
        return "- backend [subsystem] Backend :: back_end"

    async def _compose(**_kw):
        return SimpleNamespace(
            text="COMPOSED",
            agent_uid="agentX",
            agent_rev=1,
            composed_degraded=False,
            degraded_layers=(),
        )

    monkeypatch.setattr(sweep, "load_agent_prompt_body", _none)
    monkeypatch.setattr(sweep, "_workflow_prompt", _none)
    monkeypatch.setattr(sweep, "_existing_pages_listing", _pages)
    monkeypatch.setattr(sweep, "_existing_areas_listing", _areas)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", _compose
    )
    monkeypatch.setattr(sweep, "write_audit", _none)
    monkeypatch.setattr(
        sweep, "Area", SimpleNamespace(nodes=_Nodes([_subsystem_area()]))
    )


async def test_pre_dispatch_gate_raises_so_the_router_can_409(seams, monkeypatch):
    # The ONE failure that leaves this module as an exception.
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([])))
    with pytest.raises(LifecycleError):
        await sweep.run_generate_docs(repository_uid="r1")


async def test_dispatch_time_lifecycle_error_returns_200_shaped_result(seams, monkeypatch):
    async def _boom(**_kw):
        raise LifecycleError("run policy forbids cloud executors")

    monkeypatch.setattr(sweep, "trigger_run", _boom)

    # Deliberately does NOT raise — the router returns this as HTTP 200.
    result = await sweep.run_generate_docs(repository_uid="r1")

    assert result.run_uid == ""
    assert result.errors == ["generate-docs: run policy forbids cloud executors"]
    assert "no run dispatched" in result.summary


async def test_unexpected_dispatch_exception_also_returns_200_shaped_result(
    seams, monkeypatch
):
    async def _boom(**_kw):
        raise RuntimeError("neo4j unavailable")

    monkeypatch.setattr(sweep, "trigger_run", _boom)

    result = await sweep.run_generate_docs(repository_uid="r1")

    assert result.run_uid == ""
    assert result.errors == ["generate-docs: RuntimeError: neo4j unavailable"]


def test_module_docstring_states_the_two_hundred_with_errors_contract():
    """A client reading only the status code misclassifies a failed sweep as a
    success — the contract has to be written down where the functions live."""
    doc = " ".join((sweep.__doc__ or "").split())
    assert "a 200 does not mean the sweep ran" in doc
    assert "errors" in doc and "runs_dispatched" in doc
    assert "Pre-dispatch gates" in doc and "Dispatch-time failures" in doc
