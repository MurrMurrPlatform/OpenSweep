"""Per-doc audit runs — run_audit(doc_uids=[...]) fans out one ask run per page.

Regression suite for a contract that was silently discarded on every run: the
page block used to ride `default_intent`, the LOWEST-priority instructions slot,
while the same call set `prompt_body` to the repo's ask-stage prompt (which
always resolves). So the slug `read_doc` needs, the uid `write_memory` anchors
to, and `confirm_doc_current` — the only call that clears the page's stale flag
— never reached a single run. It now rides the structural slot, after every
guidance layer, where no override can displace it.

Same seam style as test_sweep_area_scoped_audit.
"""

from types import SimpleNamespace

import pytest

from domains.runs.services import sweep
from domains.runs.services.sweep import run_audit


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


def _doc(**overrides):
    fields = dict(
        uid="d1",
        repository_uid="r1",
        slug="backend/queue-workers",
        title="Queue workers",
        watch_paths=["back_end/queue"],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def seams(monkeypatch):
    captured: dict = {}

    async def fake_load(agent_uid):
        return None

    async def fake_workflow(repository_uid, stage):
        # The ask-stage prompt always resolves on a real repo — this is exactly
        # what used to shadow the page contract.
        return "ASK STAGE PROMPT"

    async def fake_compose(**kwargs):
        captured.setdefault("compose", []).append(kwargs)
        return SimpleNamespace(
            text="COMPOSED",
            agent_uid="agentX",
            agent_rev=2,
            composed_degraded=False,
            degraded_layers=(),
        )

    async def fake_trigger(**kwargs):
        captured.setdefault("trigger", []).append(kwargs)
        return SimpleNamespace(uid=f"run{len(captured['trigger'])}")

    async def fake_audit(**kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(sweep, "load_agent_prompt_body", fake_load)
    monkeypatch.setattr(sweep, "_workflow_prompt", fake_workflow)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    monkeypatch.setattr(sweep, "write_audit", fake_audit)
    return captured


async def test_page_contract_rides_structural_not_the_instructions_slot(
    seams, monkeypatch
):
    doc = _doc()
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes([doc])))

    await run_audit(repository_uid="r1", doc_uids=["d1"])

    (compose,) = seams["compose"]
    structural = compose["structural"]
    # The three things that exist nowhere else on this path.
    assert "read_doc(slug=backend/queue-workers)" in structural
    assert "anchor_uid=d1" in structural
    assert "confirm_doc_current(slug=backend/queue-workers)" in structural
    assert "back_end/queue" in structural
    # And the ask-stage prompt still owns the instructions layer — the fix must
    # not have simply swapped which one wins.
    assert compose["prompt_body"] == "ASK STAGE PROMPT"
    assert compose["agent_key"] == "ask"


async def test_findings_budget_does_not_displace_the_instructions_layer(
    seams, monkeypatch
):
    # max_findings used to be folded into custom_intent, which outranks
    # prompt_body — so an audit whose only knob was a budget shipped an
    # instructions layer of one sentence, losing both the stage prompt and the
    # page contract.
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes([_doc()])))

    await run_audit(repository_uid="r1", doc_uids=["d1"], max_findings=5)

    (compose,) = seams["compose"]
    assert not compose["custom_intent"]
    assert "File at most 5 findings" in compose["structural"]
    assert "confirm_doc_current" in compose["structural"]
    assert compose["prompt_body"] == "ASK STAGE PROMPT"


async def test_user_focus_still_wins_the_instructions_slot(seams, monkeypatch):
    # A user's own focus text is a legitimate override and must keep its slot —
    # only the budget moved.
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes([_doc()])))

    await run_audit(
        repository_uid="r1", doc_uids=["d1"], custom_intent="focus on authz"
    )

    (compose,) = seams["compose"]
    assert compose["custom_intent"] == "focus on authz"
    assert "confirm_doc_current" in compose["structural"]


async def test_per_doc_run_carries_agent_provenance(seams, monkeypatch):
    # The per-doc branch called build_intent directly, so its runs recorded no
    # agent_uid/agent_rev and could not report a degraded compose.
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes([_doc()])))

    await run_audit(repository_uid="r1", doc_uids=["d1"])

    (trigger,) = seams["trigger"]
    assert trigger["agent_uid"] == "agentX"
    assert trigger["agent_rev"] == 2
    assert trigger["stage"] == "ask"
    assert trigger["intent"] == "COMPOSED"
    assert trigger["target"] == {"doc_uids": ["d1"], "paths": ["back_end/queue"]}


async def test_each_page_gets_its_own_contract(seams, monkeypatch):
    docs = [
        _doc(),
        _doc(uid="d2", slug="frontend/routing", title="Routing", watch_paths=[]),
    ]
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes(docs)))

    await run_audit(repository_uid="r1", doc_uids=["d1", "d2"])

    first, second = seams["compose"]
    assert "slug=backend/queue-workers" in first["structural"]
    assert "slug=frontend/routing" in second["structural"]
    # A page with no watch_paths still gets a usable instruction.
    assert "no watch paths" in second["structural"]
