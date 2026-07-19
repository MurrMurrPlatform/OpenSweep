"""Org overrides of system agents — validation, monotonic revisions, delete
tombstones, revert, tenancy, audit, resolution degradation, and intent
composition. DB-free: the neomodel classes are monkeypatched with in-memory
fakes (same pattern as the LLM-provider tenancy tests)."""

import asyncio

import pytest
from fastapi import HTTPException

import domains.agents.services.agent_service as svc
import domains.agents.services.registry as registry
from domains.agents.models import AGENT_PROMPT_MAX_BYTES
from domains.agents.services import composition
from domains.agents.services.seed_agent_bases import agent_base_fallback

# ── In-memory fakes ──────────────────────────────────────────────────────────


class _Node:
    _defaults: dict = {}
    _store: list

    def __init__(self, **kw):
        values = dict(self._defaults)
        values.update(kw)
        self.__dict__.update(values)

    async def save(self):
        if self not in self._store:
            self._store.append(self)
        return self

    async def delete(self):
        self._store.remove(self)


class _Nodes:
    def __init__(self, store):
        self._store = store

    async def all(self):
        return list(self._store)

    async def filter(self, **kw):
        return [
            n for n in self._store
            if all(getattr(n, k, None) == v for k, v in kw.items())
        ]

    async def get_or_none(self, **kw):
        rows = await self.filter(**kw)
        return rows[0] if rows else None


_AGENTS: list = []
_REVISIONS: list = []


class FakeAgent(_Node):
    _store = _AGENTS
    _defaults = dict(
        uid="", org_uid="", title="", description="", prompt="",
        produces="findings", default_effort="normal", default_executor="",
        tags=[], provenance="user", source_url="", source_commit="",
        seed_checksum="", rev=0, enabled=True, created_at=None, updated_at=None,
    )
    nodes = _Nodes(_AGENTS)


class FakeRevision(_Node):
    _store = _REVISIONS
    _defaults = dict(
        uid="", agent_uid="", org_uid="", rev=0, mode="replace", body="",
        enabled=True, author_uid="", created_at=None,
    )
    nodes = _Nodes(_REVISIONS)


AUDITS: list[dict] = []


@pytest.fixture(autouse=True)
def fake_agents(monkeypatch):
    _AGENTS.clear()
    _REVISIONS.clear()
    AUDITS.clear()
    monkeypatch.setattr(svc, "Agent", FakeAgent)
    monkeypatch.setattr(svc, "AgentRevision", FakeRevision)
    # composition/provenance resolve the system row through the registry.
    monkeypatch.setattr(registry, "Agent", FakeAgent)

    async def capture_audit(**kw):
        AUDITS.append(kw)

    monkeypatch.setattr(svc, "write_audit", capture_audit)
    svc._REV_LOCKS.clear()
    yield
    _AGENTS.clear()
    _REVISIONS.clear()


def _system_agent(key="review", *, provenance="system", enabled=True) -> FakeAgent:
    agent = FakeAgent(
        uid=f"agent-{key}",
        title=f"OpenSweep agent — {key}",
        prompt=f"Seeded {key} instructions.",
        provenance=provenance,
        source_url=f"opensweep://agent/{key}",
        enabled=enabled,
    )
    _AGENTS.append(agent)
    return agent


async def _put(agent_uid="agent-review", org="org-a", mode="append",
               body="Guidance.", enabled=True, actor="u1"):
    return await svc.save_override(
        agent_uid=agent_uid, org_uid=org, mode=mode, body=body,
        enabled=enabled, actor_uid=actor,
    )


# ── Validation ───────────────────────────────────────────────────────────────


async def test_invalid_mode_is_422():
    _system_agent()
    with pytest.raises(HTTPException) as exc:
        await _put(mode="prepend")
    assert exc.value.status_code == 422


async def test_body_over_32kb_is_422_with_clear_message():
    _system_agent()
    with pytest.raises(HTTPException) as exc:
        await _put(body="x" * (AGENT_PROMPT_MAX_BYTES + 1))
    assert exc.value.status_code == 422
    assert "32 KB" in exc.value.detail


async def test_body_at_the_cap_is_accepted():
    _system_agent()
    rev = await _put(body="x" * AGENT_PROMPT_MAX_BYTES)
    assert rev.rev == 1


async def test_orgless_caller_is_422():
    _system_agent()
    with pytest.raises(HTTPException) as exc:
        await _put(org="")
    assert exc.value.status_code == 422


async def test_override_on_non_system_agent_is_422():
    _system_agent(provenance="user")
    with pytest.raises(HTTPException) as exc:
        await _put()
    assert exc.value.status_code == 422


# ── Uniqueness under concurrent upsert ───────────────────────────────────────


async def test_concurrent_saves_yield_unique_monotonic_revs():
    _system_agent()
    await asyncio.gather(*[_put(body=f"v{i}") for i in range(6)])
    revs = sorted(int(r.rev) for r in _REVISIONS)
    assert revs == [1, 2, 3, 4, 5, 6]  # every save snapshotted, revs unique


# ── Revision monotonicity + delete tombstones ────────────────────────────────


async def test_delete_appends_a_disabled_tombstone_and_revs_continue():
    agent = _system_agent()
    await _put(body="v1")
    await _put(body="v2")
    await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="u1")
    assert len(_REVISIONS) == 3  # history kept, tombstone appended
    head = max(_REVISIONS, key=lambda r: int(r.rev))
    assert int(head.rev) == 3
    assert head.enabled is False and head.body == ""
    assert AUDITS[-1]["kind"] == "agent_override.deleted"
    # The active override is gone…
    assert await svc.resolve_enabled_override("org-a", agent) is None
    # …and the rev sequence continues across the delete.
    rev = await _put(body="v3")
    assert int(rev.rev) == 4


async def test_delete_without_override_is_404():
    agent = _system_agent()
    with pytest.raises(HTTPException) as exc:
        await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="u1")
    assert exc.value.status_code == 404


async def test_delete_twice_is_404_the_second_time():
    agent = _system_agent()
    await _put(body="v1")
    await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="u1")
    with pytest.raises(HTTPException) as exc:
        await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="u1")
    assert exc.value.status_code == 404


# ── Revert ───────────────────────────────────────────────────────────────────


async def test_revert_creates_a_new_head_copying_the_old_revision():
    agent = _system_agent()
    await _put(body="v1", mode="append")
    await _put(body="v2", mode="replace")
    head = await svc.revert_override(
        agent_uid=agent.uid, org_uid="org-a", rev=1, actor_uid="u2"
    )
    assert int(head.rev) == 3  # new head, history append-only
    assert head.body == "v1"
    assert head.mode == "append"
    assert head.enabled is True
    assert head.author_uid == "u2"
    revs = await svc.list_revisions(agent.uid, org_uid="org-a", include_platform=False)
    assert [r.rev for r in revs] == [3, 2, 1]
    assert AUDITS[-1]["kind"] == "agent_override.reverted"
    assert AUDITS[-1]["payload"]["reverted_to_rev"] == 1


async def test_revert_to_missing_rev_is_404():
    agent = _system_agent()
    await _put(body="v1")
    with pytest.raises(HTTPException) as exc:
        await svc.revert_override(agent_uid=agent.uid, org_uid="org-a", rev=99, actor_uid="u1")
    assert exc.value.status_code == 404


# ── Tenancy isolation ────────────────────────────────────────────────────────


async def test_org_a_never_sees_or_affects_org_b():
    agent = _system_agent()
    await _put(org="org-b", body="theirs")
    # read isolation
    assert await svc.resolve_enabled_override("org-a", agent) is None
    assert await svc.list_revisions(agent.uid, org_uid="org-a", include_platform=False) == []
    # write isolation: a delete scoped to org-a cannot touch org-b's override
    with pytest.raises(HTTPException):
        await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="ua")
    assert len(_REVISIONS) == 1 and _REVISIONS[0].org_uid == "org-b"


async def test_resolution_is_scoped_to_the_org():
    agent = _system_agent()
    await _put(org="org-a", body="mine")
    await _put(org="org-b", body="theirs")
    mine = await svc.resolve_enabled_override("org-a", agent)
    assert mine.body == "mine"


# ── Resolution degrades, never raises ────────────────────────────────────────


async def test_disabled_override_resolves_to_none():
    agent = _system_agent()
    await _put(enabled=False)
    assert await svc.resolve_enabled_override("org-a", agent) is None


async def test_empty_body_override_resolves_to_none():
    agent = _system_agent()
    await _put(body="   ")
    assert await svc.resolve_enabled_override("org-a", agent) is None


async def test_resolution_failure_degrades_to_none(monkeypatch):
    agent = _system_agent()

    class Broken:
        class nodes:
            @staticmethod
            async def filter(**kw):
                raise RuntimeError("db down")

    monkeypatch.setattr(svc, "AgentRevision", Broken)
    assert await svc.resolve_enabled_override("org-a", agent) is None


async def test_provenance_lookup_failure_degrades_to_empty(monkeypatch):
    class Broken:
        class nodes:
            @staticmethod
            async def filter(**kw):
                raise RuntimeError("db down")

    monkeypatch.setattr(registry, "Agent", Broken)
    assert await svc.active_agent_provenance("org-a", "review") == ("", 0)


# ── Audit + attribution ──────────────────────────────────────────────────────


async def test_mutations_write_audit_with_actor():
    agent = _system_agent()
    await _put(actor="alice")
    await svc.revert_override(agent_uid=agent.uid, org_uid="org-a", rev=1, actor_uid="bob")
    await svc.delete_override(agent_uid=agent.uid, org_uid="org-a", actor_uid="carol")
    kinds = [a["kind"] for a in AUDITS]
    assert kinds == [
        "agent_override.updated",
        "agent_override.reverted",
        "agent_override.deleted",
    ]
    assert [a["actor_uid"] for a in AUDITS] == ["alice", "bob", "carol"]
    assert AUDITS[1]["payload"]["reverted_to_rev"] == 1


# ── Preview does not persist ─────────────────────────────────────────────────


async def test_preview_composes_without_persisting(monkeypatch):
    async def fake_base(agent_key):
        return "Seeded review instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    prompt = await composition.preview_composed_prompt(
        org_uid="org-a", agent_key="review", mode="append", body="Draft org guidance."
    )
    assert "Seeded review instructions." in prompt
    assert "## Organization guidance" in prompt
    assert "Draft org guidance." in prompt
    assert "You are OpenSweep" in prompt  # header
    assert "# Look-before-write contract" in prompt  # footer
    assert _REVISIONS == []  # nothing persisted
    assert AUDITS == []  # preview is not a mutation


async def test_preview_replace_substitutes_the_platform_layer(monkeypatch):
    async def fake_base(agent_key):
        return "Seeded review instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    prompt = await composition.preview_composed_prompt(
        org_uid="org-a", agent_key="review", mode="replace", body="Org replacement."
    )
    assert "Org replacement." in prompt
    assert "Seeded review instructions." not in prompt
    assert "# Look-before-write contract" in prompt


# ── Dispatch-path composition (compose_agent_intent) ─────────────────────────


async def test_compose_places_org_guidance_between_platform_and_repo_layers(monkeypatch):
    """The wiring the review trigger uses: platform instructions → org
    guidance → repo stage guidance → structural contract, with provenance."""

    async def fake_base(agent_key):
        return "Seeded review instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    agent = _system_agent("review")
    await _put(agent_uid=agent.uid, mode="append", body="Org review guidance.")
    composed = await composition.compose_agent_intent(
        repository_uid="repo-1",
        agent_key="review",
        stage="review",
        repo_guidance="Repo review guidance body.",
        structural="Review pull request #7 and finish with a verdict.",
        org_uid="org-a",
    )
    out = composed.text
    assert out.index("You are OpenSweep") < out.index("Seeded review instructions.")
    assert out.index("Seeded review instructions.") < out.index("## Organization guidance")
    assert out.index("## Organization guidance") < out.index("Repo review guidance body.")
    assert out.index("Repo review guidance body.") < out.index("Review pull request #7")
    assert out.index("Review pull request #7") < out.index("# Look-before-write contract")
    # Provenance for the Run row (recorded at dispatch).
    assert composed.agent_uid == agent.uid
    assert composed.agent_rev == 1


async def test_compose_replace_keeps_repo_guidance_and_structural(monkeypatch):
    async def fake_base(agent_key):
        return "Seeded fix instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    agent = _system_agent("fix")
    await _put(agent_uid=agent.uid, mode="replace", body="Org replacement instructions.")
    composed = await composition.compose_agent_intent(
        repository_uid="repo-1",
        agent_key="fix",
        stage="fix",
        repo_guidance="Repo fix guidance body.",
        structural="Fix the blocking findings on PR #9.",
        org_uid="org-a",
    )
    out = composed.text
    assert "Org replacement instructions." in out
    assert "Seeded fix instructions." not in out
    assert "Repo fix guidance body." in out  # replace never removes repo guidance
    assert "Fix the blocking findings on PR #9." in out  # …or the structural contract
    assert "# Look-before-write contract" in out  # …or the footer


async def test_custom_intent_beats_a_replace_override(monkeypatch):
    """A run-specific instruction override takes the instructions slot; a
    replace override never substitutes it."""

    async def fake_base(agent_key):
        return "Seeded review instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    agent = _system_agent("review")
    await _put(agent_uid=agent.uid, mode="replace", body="Org replacement.")
    composed = await composition.compose_agent_intent(
        repository_uid="repo-1",
        agent_key="review",
        custom_intent="Custom run instruction.",
        structural="Review pull request #7.",
        org_uid="org-a",
    )
    assert "Custom run instruction." in composed.text
    assert "Org replacement." not in composed.text


async def test_compose_degrades_when_every_layer_is_broken(monkeypatch):
    """No base row, broken agent store — the run still gets a usable intent
    from the in-code fallback."""

    async def broken_base(agent_key):
        return None

    class Broken:
        class nodes:
            @staticmethod
            async def filter(**kw):
                raise RuntimeError("db down")

    monkeypatch.setattr(composition, "_resolve_platform_base", broken_base)
    monkeypatch.setattr(registry, "Agent", Broken)
    composed = await composition.compose_agent_intent(
        repository_uid="repo-1",
        agent_key="verify",
        stage="verify",
        repo_guidance="",
        structural="Verify finding X.",
        org_uid="org-a",
    )
    assert agent_base_fallback("verify") in composed.text
    assert "Verify finding X." in composed.text
    assert composed.agent_uid == "" and composed.agent_rev == 0


async def test_chat_layers_have_no_header_or_footer(monkeypatch):
    async def fake_base(agent_key):
        return "Platform chat instructions."

    monkeypatch.setattr(composition, "_resolve_platform_base", fake_base)
    agent = _system_agent("chat")
    await _put(agent_uid=agent.uid, mode="append", body="Org chat guidance.")
    layers = await composition.chat_instruction_layers("org-a")
    assert "Platform chat instructions." in layers
    assert "## Organization guidance" in layers
    assert "Org chat guidance." in layers
    assert "You are OpenSweep, an agent analyzing" not in layers  # no framing header
    assert "# Look-before-write contract" not in layers  # no footer
