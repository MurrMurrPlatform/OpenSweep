"""Run.surface — hidden agent surfaces (comment replies, chat-bubble
sessions) stay off the Runs page and out of the org-wide active poll.

DB-free: route functions are called directly with faked collaborators,
matching test_run_create_tenancy.py.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from domains.comments.schemas import CommentSubjectType
from domains.runs.models import RUN_SURFACES, Run
from domains.runs.schemas import CreateRunRequest, Playbook, RunDTO
from domains.runs.services.turn_service import run_to_dto
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


# ── model / DTO contract ─────────────────────────────────────────────────────


def test_surfaces_are_runs_comment_chat_slack():
    assert RUN_SURFACES == {"runs", "comment", "chat", "slack"}


def test_dto_defaults_surface_to_runs():
    assert RunDTO(uid="r", repository_uid="repo", executor="internal_llm").surface == "runs"


def test_run_to_dto_maps_missing_surface_to_runs():
    run = Run(uid="r1", repository_uid="repo1", executor="internal_llm")
    run.surface = None  # node predating the field
    assert run_to_dto(run).surface == "runs"
    run.surface = "comment"
    assert run_to_dto(run).surface == "comment"


# ── effort / reasoning stamping ──────────────────────────────────────────────


def test_run_to_dto_maps_effort_and_reasoning():
    run = Run(uid="r1", repository_uid="repo1", executor="internal_llm")
    run.effort = "deep"
    run.reasoning = "high"
    dto = run_to_dto(run)
    assert dto.effort == "deep"
    assert dto.reasoning == "high"


def test_run_to_dto_defaults_effort_and_reasoning_to_empty():
    run = Run(uid="r1", repository_uid="repo1", executor="internal_llm")
    run.effort = None  # node predating the fields
    run.reasoning = None
    dto = run_to_dto(run)
    assert dto.effort == ""
    assert dto.reasoning == ""


async def test_dispatch_agent_stamps_effort_and_reasoning(monkeypatch):
    from domains.agents.models import Agent
    from domains.agents.services import dispatch as dispatch_module
    from domains.run_policies.services import effort as effort_module
    from domains.runs.services import lifecycle

    captured: dict = {}

    async def fake_trigger_run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(uid="run-1")

    async def fake_policy(tier):
        captured["policy_tier"] = tier
        return SimpleNamespace(uid="policy-1")

    async def fake_compose(**kwargs):
        return SimpleNamespace(
            text="intent",
            agent_uid="a1",
            agent_rev=0,
            composed_degraded=False,
            degraded_layers=(),
        )

    monkeypatch.setattr(lifecycle, "trigger_run", fake_trigger_run)
    monkeypatch.setattr(effort_module, "ensure_policy_for_effort", fake_policy)
    monkeypatch.setattr(dispatch_module, "compose_agent_intent", fake_compose)

    agent = Agent(
        uid="a1", org_uid="org-a", title="Deep audit", produces="findings",
        default_effort="deep", reasoning="", enabled=True,
    )
    await dispatch_module.dispatch_agent(agent=agent, repository_uid="repo1")
    assert captured["effort"] == "deep"
    assert captured["reasoning"] == "high"  # deep tier default

    # An explicit agent reasoning override wins over the tier default.
    agent.reasoning = "low"
    await dispatch_module.dispatch_agent(agent=agent, repository_uid="repo1")
    assert captured["reasoning"] == "low"


# ── trigger_run guard ────────────────────────────────────────────────────────


async def test_trigger_run_rejects_unknown_surface():
    from domains.runs.services.lifecycle import LifecycleError, trigger_run

    with pytest.raises(LifecycleError, match="unknown surface"):
        await trigger_run(
            repository_uid="repo1", intent="do it", playbook="ask", surface="bogus"
        )


# ── @opensweep dispatch carries the surface + generic subject keys ───────────────


async def test_opensweep_reply_dispatch_sets_comment_surface(monkeypatch):
    from domains.comments import opensweep_mention
    from domains.comments.models import Comment
    from domains.runs.services import lifecycle
    from domains.tickets.models import Ticket

    captured: dict = {}

    async def fake_trigger_run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(uid="run-1")

    async def fake_thread(*a, **k):
        return ""

    async def fake_mentioned(*a, **k):
        return ""

    async def fake_policy(*a, **k):
        return SimpleNamespace(uid="policy-1")

    monkeypatch.setattr(lifecycle, "trigger_run", fake_trigger_run)
    monkeypatch.setattr(opensweep_mention, "render_thread", fake_thread)
    monkeypatch.setattr(opensweep_mention, "render_mentioned_items", fake_mentioned)
    monkeypatch.setattr(opensweep_mention, "ensure_policy_for_effort", fake_policy)

    ticket = Ticket(
        uid="t1", repository_uid="repo1", title="Fix login", description="d",
        status="backlog", priority="high",
    )
    comment = Comment(
        uid="c1", subject_type="ticket", subject_uid="t1",
        author_uid="u1", body="@opensweep refine this",
    )
    run_uid = await opensweep_mention.trigger_opensweep_reply(
        comment, CommentSubjectType.TICKET, ticket
    )
    assert run_uid == "run-1"
    assert captured["surface"] == "comment"
    assert captured["target"]["subject_type"] == "ticket"
    assert captured["target"]["subject_uid"] == "t1"
    assert captured["target"]["comment_uid"] == "c1"
    assert captured["target"]["ticket_uid"] == "t1"


def test_pending_filter_matches_only_this_subject():
    from domains.comments.opensweep_mention import filter_pending_for_subject

    def run(uid, subject_type="ticket", subject_uid="t1"):
        return SimpleNamespace(
            uid=uid, target={"subject_type": subject_type, "subject_uid": subject_uid}
        )

    runs = [run("r1"), run("r2", subject_uid="t2"), run("r3", subject_type="finding")]
    matched = filter_pending_for_subject(runs, CommentSubjectType.TICKET, "t1")
    assert [r.uid for r in matched] == ["r1"]

    legacy = SimpleNamespace(uid="r4", target=None)  # pre-surface node
    assert filter_pending_for_subject([legacy], CommentSubjectType.TICKET, "t1") == []


# ── GET /runs surface gate ───────────────────────────────────────────────────


def _user(role="maintainer", platform_admin=False, uid="u1"):
    return UserDTO(
        uid=uid, email="e@x.y", display_name="U", role=role,
        org_uid="org-a", is_platform_admin=platform_admin,
    )


def _node(uid, surface="runs", triggered_by="u1", repository_uid="repo1"):
    now = datetime.now(UTC)
    return Run(
        uid=uid, repository_uid=repository_uid, executor="internal_llm",
        surface=surface, triggered_by=triggered_by, status="ended",
        last_activity_at=now, started_at=now, created_at=now,
    )


@pytest.fixture
def runs_api(monkeypatch):
    import api.v1.runs as runs_module

    legacy = _node("legacy")
    legacy.surface = None  # node predating the surface field
    nodes = [
        _node("visible"),
        legacy,
        _node("reply", surface="comment"),
        _node("mine", surface="chat", triggered_by="u1"),
        _node("theirs", surface="chat", triggered_by="u2"),
    ]

    async def fake_all():
        return nodes

    async def fake_filter(**kwargs):
        def ok(n):
            for k, v in kwargs.items():
                if k.endswith("__in"):
                    if getattr(n, k[:-4]) not in v:
                        return False
                elif getattr(n, k) != v:
                    return False
            return True

        return [n for n in nodes if ok(n)]

    async def fake_repo_scope(repository_uid, org_uid):
        return [repository_uid] if repository_uid else ["repo1"]

    monkeypatch.setattr(
        runs_module,
        "Run",
        SimpleNamespace(nodes=SimpleNamespace(all=fake_all, filter=fake_filter)),
    )
    monkeypatch.setattr(runs_module, "repo_scope", fake_repo_scope)
    return runs_module


async def _uids(runs_module, **kwargs):
    defaults = dict(
        response=Response(),
        repository_uid=None, executor=None, status=None, playbook=None,
        linked_pr_uid=None, linked_ticket_uid=None, linked_finding_uid=None,
        surface=None, limit=100, offset=0,
    )
    defaults.update(kwargs)
    return [r.uid for r in await runs_module.list_runs(**defaults)]


async def test_default_list_hides_agent_surfaces(runs_api):
    # Legacy nodes (surface=None) must stay visible — they're normal runs.
    assert await _uids(runs_api, user=_user()) == ["visible", "legacy"]


async def test_comment_surface_needs_platform_admin(runs_api):
    with pytest.raises(HTTPException) as exc:
        await _uids(runs_api, surface="comment", user=_user())
    assert exc.value.status_code == 403
    assert await _uids(runs_api, surface="comment", user=_user(platform_admin=True)) == ["reply"]


async def test_all_surface_needs_platform_admin(runs_api):
    with pytest.raises(HTTPException) as exc:
        await _uids(runs_api, surface="all", user=_user())
    assert exc.value.status_code == 403
    assert set(await _uids(runs_api, surface="all", user=_user(platform_admin=True))) == {
        "visible", "legacy", "reply", "mine", "theirs",
    }


async def test_chat_surface_returns_only_own_sessions(runs_api):
    # "chat" is the widget's personal history — own sessions for EVERYONE,
    # platform admins included (oversight goes through surface=all).
    assert await _uids(runs_api, surface="chat", user=_user()) == ["mine"]
    assert await _uids(runs_api, surface="chat", user=_user(platform_admin=True)) == ["mine"]


async def test_unknown_surface_is_422(runs_api):
    with pytest.raises(HTTPException) as exc:
        await _uids(runs_api, surface="bogus", user=_user())
    assert exc.value.status_code == 422


async def test_active_runs_exclude_agent_surfaces(runs_api, monkeypatch):
    async def fake_active(**kwargs):
        return [
            _node("visible", surface="runs"),
            _node("reply", surface="comment"),
            _node("mine", surface="chat"),
        ]

    monkeypatch.setattr(runs_api, "active_runs_for", fake_active)
    out = await runs_api.list_active_runs(
        repository_uid=None, pull_request_uid=None, ticket_uid=None,
        finding_uid=None, playbook=None, user=_user(),
    )
    assert [r.run_uid for r in out] == ["visible"]


# ── POST /runs surface validation ────────────────────────────────────────────


async def test_create_run_rejects_bad_surface_combos(monkeypatch):
    import api.v1.runs as runs_module

    async def fake_require(repo, org):
        return None

    monkeypatch.setattr(runs_module, "require_repo_in_org", fake_require)

    with pytest.raises(HTTPException) as exc:
        await runs_module.create_run(
            CreateRunRequest(repository_uid="repo1", playbook=Playbook.ASK, surface="chat"),
            user=_user(),
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await runs_module.create_run(
            CreateRunRequest(repository_uid="repo1", playbook=Playbook.CHAT, surface="comment"),
            user=_user(),
        )
    assert exc.value.status_code == 422


async def test_create_chat_without_repo_or_context_is_422(monkeypatch):
    import api.v1.runs as runs_module

    with pytest.raises(HTTPException) as exc:
        await runs_module.create_run(
            CreateRunRequest(playbook=Playbook.CHAT, surface="chat"), user=_user()
        )
    assert exc.value.status_code == 422
    assert "repository_uid" in exc.value.detail


async def test_create_chat_resolves_repo_from_context_subject(monkeypatch):
    import api.v1.runs as runs_module
    from domains.comments import subjects

    seen: dict = {}

    async def fake_get_subject(subject_type, uid):
        return SimpleNamespace(uid=uid, repository_uid="repo-from-subject")

    async def fake_require(repo, org):
        seen["repo"] = repo

    async def fake_chat(req, *, actor_uid, org_uid):
        seen["req_repo"] = req.repository_uid
        return Run(uid="r1", repository_uid=req.repository_uid, executor="internal_llm")

    monkeypatch.setattr(subjects, "get_subject", fake_get_subject)
    monkeypatch.setattr(runs_module, "require_repo_in_org", fake_require)
    monkeypatch.setattr(runs_module, "_create_chat_run", fake_chat)

    await runs_module.create_run(
        CreateRunRequest(
            playbook=Playbook.CHAT,
            surface="chat",
            context={"subject_type": "ticket", "subject_uid": "t1"},
        ),
        user=_user(),
    )
    assert seen["repo"] == "repo-from-subject"
    assert seen["req_repo"] == "repo-from-subject"


# ── chat preamble ────────────────────────────────────────────────────────────


async def test_chat_preamble_without_context_is_just_the_contract():
    from domains.runs.services.chat_context import build_chat_preamble

    text = await build_chat_preamble({})
    assert "opensweep_platform_" in text
    assert "read-only" in text
    assert "viewing the following" not in text


async def test_chat_preamble_snapshots_the_subject(monkeypatch):
    from domains.comments import subjects
    from domains.runs.services.chat_context import build_chat_preamble

    async def fake_get_subject(subject_type, uid):
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(subjects, "get_subject", fake_get_subject)
    monkeypatch.setattr(
        subjects, "subject_snapshot", lambda st, s: f"SNAPSHOT {st.value} {s.uid}"
    )
    text = await build_chat_preamble({"subject_type": "ticket", "subject_uid": "t9"})
    assert "SNAPSHOT ticket t9" in text


async def test_chat_preamble_survives_a_missing_subject(monkeypatch):
    from domains.comments import subjects
    from domains.runs.services.chat_context import build_chat_preamble

    async def fake_get_subject(subject_type, uid):
        return None

    monkeypatch.setattr(subjects, "get_subject", fake_get_subject)
    text = await build_chat_preamble({"subject_type": "ticket", "subject_uid": "gone"})
    assert "opensweep_platform_" in text
    assert "viewing the following" not in text


# ── route surface ────────────────────────────────────────────────────────────


def test_pending_opensweep_runs_route_is_mounted():
    from app import app

    paths = app.openapi()["paths"]
    assert "/api/v1/comments/pending-opensweep-runs" in paths
    op = paths["/api/v1/comments/pending-opensweep-runs"]["get"]
    assert op["operationId"] == "opensweep_comment_pending_runs"



# ── the list repairs the rows it fetched ─────────────────────────────────────
#
# WHY: reconcile_stale_runs() used to run inline here, scanning every
# repairable Run across every tenant on each page load. Dropping it left the
# 5-minute beat tick as the only repair for this view, so a run whose
# dispatching process had died could sit in the list reading "running" for
# minutes. The list now repairs only the rows it already holds — bounded by
# the caller's own in-flight runs, no extra query, no cross-tenant read.


def _one_stale_row(monkeypatch, runs_module):
    """Point the route at a single queued/running row, and make the repair
    fail it the way _fail_run does — in place, on the same object."""
    stale = _node("stuck")
    stale.status = "running"

    async def only_stale(**kwargs):
        return [stale]

    async def fake_reconcile(runs, **kwargs):
        for r in runs:
            r.status = "failed"

    monkeypatch.setattr(
        runs_module, "Run", SimpleNamespace(nodes=SimpleNamespace(filter=only_stale))
    )
    monkeypatch.setattr(runs_module, "reconcile_runs", fake_reconcile)
    return stale


async def test_the_list_reports_a_repaired_row_as_failed(runs_api, monkeypatch):
    _one_stale_row(monkeypatch, runs_api)
    out = await runs_api.list_runs(
        response=Response(), repository_uid=None, executor=None, status=None,
        playbook=None, linked_pr_uid=None, linked_ticket_uid=None,
        linked_finding_uid=None, surface=None, limit=100, offset=0, user=_user(),
    )
    assert [(r.uid, r.status) for r in out] == [("stuck", "failed")]


async def test_a_status_filter_drops_a_row_the_repair_just_failed(runs_api, monkeypatch):
    """?status=running must not come back holding a run just marked failed.

    The status predicate is pushed into Cypher, so the row matched when it was
    fetched; reconcile_runs then moved it. Without the re-check the caller
    gets a "running" list containing a failed run.
    """
    _one_stale_row(monkeypatch, runs_api)
    assert await _uids(runs_api, user=_user(), status="running") == []
    assert await _uids(runs_api, user=_user()) == ["stuck"]


async def test_the_repair_only_sees_rows_already_fetched(runs_api, monkeypatch):
    """Bounded by this request's own result set — never a fresh unbounded read."""
    seen: list[list] = []

    async def fake_reconcile(runs, **kwargs):
        seen.append(list(runs))

    monkeypatch.setattr(runs_api, "reconcile_runs", fake_reconcile)
    await _uids(runs_api, user=_user())
    assert len(seen) == 1
    assert {r.uid for r in seen[0]} == {"visible", "legacy", "reply", "mine", "theirs"}


async def test_scheduled_agent_run_history_repairs_its_own_rows(monkeypatch):
    """Same self-heal on GET /scheduled-agents/{uid}/runs.

    The agent's history is where someone checks whether last night's run
    worked; a row orphaned by a restart must not read "running" there until
    the beat tick comes round.
    """
    import api.v1.scheduled_agents as agents_api
    import domains.runs.models as run_models
    import domains.runs.services.run_reconciliation as reconciliation
    from domains.agents.services import scheduled_agent_service

    stale = _node("stuck")
    stale.status = "running"
    stale.scheduled_agent_uid = "sa1"

    async def fake_filter(**kwargs):
        assert kwargs == {"scheduled_agent_uid": "sa1"}  # scoped, not a scan
        return [stale]

    async def fake_get_model(uid):
        return SimpleNamespace(uid=uid, repository_uid="repo1")

    async def fake_require(repo, org):
        return None

    repaired: list[list] = []

    async def fake_reconcile(runs, **kwargs):
        repaired.append(list(runs))
        for r in runs:
            r.status = "failed"

    monkeypatch.setattr(run_models.Run, "nodes", SimpleNamespace(filter=fake_filter))
    monkeypatch.setattr(scheduled_agent_service, "get_scheduled_agent_model", fake_get_model)
    monkeypatch.setattr(agents_api, "require_repo_in_org", fake_require)
    monkeypatch.setattr(reconciliation, "reconcile_runs", fake_reconcile)

    out = await agents_api.list_runs(uid="sa1", user=_user())
    assert [(r.uid, r.status) for r in out] == [("stuck", "failed")]
    assert [r.uid for r in repaired[0]] == ["stuck"]
