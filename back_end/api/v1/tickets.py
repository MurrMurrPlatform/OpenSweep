"""Ticket routes — board CRUD, Gate-1 transition, finding/PR links (§15 Phase 2).

Gate 1 (backlog → todo) is the human approval gate: maintainer+ only, and it
records approved_by/approved_at. Status only ever moves through the transition
endpoint so every move is legality-checked and audited.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.pagination import Page, page_params, paginate
from domains.tenancy import repo_scope, require_repo_in_org
from domains.tickets.schemas import (
    CreateEpicRequest,
    CreateTicketRequest,
    LinkFindingRequest,
    LinkPullRequestRequest,
    PlanEpicsRequest,
    TriageBatchRequest,
    TicketDetailDTO,
    TicketDTO,
    TicketStatus,
    TransitionTicketRequest,
    UpdateTicketRequest,
)
from domains.tickets.services.epics.axes import partition
from domains.tickets.services.epics.schemas import (
    MAX_RATIONALE_CHARS,
    EpicAxis,
    EpicDraft,
    EpicPartition,
    EpicSelection,
    TicketFacts,
)
from domains.tickets.services.epics.selection import select_tickets
from domains.tickets.services.ticket_service import TicketService, ticket_to_dto
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketDTO], operation_id="opensweep_ticket_list")
async def list_tickets(
    response: Response,
    repository_uid: str | None = Query(None),
    status: str | None = Query(None),
    origin: str | None = Query(None),
    parent_ticket_uid: str | None = Query(None),
    assignee_uid: str | None = Query(None),
    archived: bool = Query(False, description="true = archived tickets only"),
    page: Page = Depends(page_params),
    user: UserDTO = Depends(get_current_user),
):
    """Priority desc, then updated_at desc."""
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    tickets = await TicketService().list(
        repository_uids=await repo_scope(repository_uid, user.org_uid),
        status=status,
        origin=origin,
        parent_ticket_uid=parent_ticket_uid,
        assignee_uid=assignee_uid,
        archived=archived,
    )
    return paginate(tickets, page, response)


# ── Grouping — epic related tickets under one parent ───────────────────────


@router.post("/epics", response_model=TicketDTO, operation_id="opensweep_ticket_create_epic")
async def create_epic(
    req: CreateEpicRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    """Group ≥2 tickets under a new parent ticket so the epic can be
    approved and implemented as one unit. Members keep their own status; the
    parent is born in backlog (Gate 1 stays human-only)."""
    await require_repo_in_org(req.repository_uid, user.org_uid)
    parent = await TicketService().create_epic(
        repository_uid=req.repository_uid,
        title=req.title,
        description=req.description,
        member_ticket_uids=req.member_ticket_uids,
        labels=req.labels,
        priority=req.priority,
        origin="human",
        actor_uid=user.uid,
    )
    return ticket_to_dto(parent)


class SuggestEpicsRequest(BaseModel):
    """Dispatch the grouping agent over a slice of the board.

    Selection mirrors `PlanEpicsRequest` field for field, so "the top 20
    criticals in the auth area" means the same thing whether a rule or an agent
    groups them — and so the prompt stops growing with the whole backlog.
    """

    repository_uid: str = Field(min_length=1)
    #: Which kinds of togetherness to ask for — keys of `_GROUPING_GOALS`.
    goals: list[str] = Field(default_factory=lambda: [EpicAxis.ROOT_CAUSE.value])
    #: conservative | balanced | exhaustive — how readily the agent epics.
    aggressiveness: str = "balanced"
    # Selection — same vocabulary as the rule builder.
    statuses: list[str] = Field(default_factory=lambda: ["backlog", "todo"])
    min_priority: str = ""
    min_severity: str = ""
    labels: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    area_keys: list[str] = Field(default_factory=list)
    #: Wider than the rule builder's 20: a cross-cutting group only becomes
    #: visible when enough of the board is in front of the model at once. 0
    #: takes every match, which on a large board is a very long prompt.
    limit: int = Field(default=40, ge=0)
    sort: str = "priority"


@dataclass(frozen=True)
class _Goal:
    """One kind of togetherness the agent may be asked to find.

    Each goal is one block in the prompt AND one axis on the resulting
    proposal, so a reviewer can tell which claim they are approving when a
    single run was asked for several.
    """

    axis: EpicAxis
    block: str


# The three axes arithmetic cannot reach. Every other axis is deliberately
# absent: the platform computes those exactly and instantly (see
# `epics.axes`), so an agent proposing one costs a human review for a grouping
# they could have had for free.
#
# `theme` and `co-change` are NOT the model re-doing `area`/`files`. Those are
# computed from `Finding.affected_paths`; a ticket with no finding, or a
# finding with no paths, carries no area key and no paths and is therefore
# absent from every computed cluster no matter how obviously it belongs. That
# blind spot is what these two goals exist to cover.
_GROUPING_GOALS: dict[str, _Goal] = {
    EpicAxis.ROOT_CAUSE.value: _Goal(
        axis=EpicAxis.ROOT_CAUSE,
        block=(
            "**ROOT CAUSE** — one defect, one missing abstraction, or one bad "
            "assumption that is showing up in several places, so a single fix "
            "removes every symptom instead of patching each one in its own "
            "PR. This is the axis arithmetic cannot reach at all: the members "
            "may share no files, no area and no words. Look for the same "
            "helper misused at five call sites, one missing validation that "
            "several items each work around, one type that should have made a "
            "class of bugs impossible. Stamp `axis='root-cause'` and name the "
            "cause in `evidence.root_cause`."
        ),
    ),
    EpicAxis.THEME.value: _Goal(
        axis=EpicAxis.THEME,
        block=(
            "**THEME** — one feature, capability, or user-facing surface. The "
            "causes differ and so do the fixes, but the work is one coherent "
            "thing to hold in your head, so one run that does all of it beats "
            "three runs each re-learning the same code. Stamp `axis='theme'` "
            "and name the capability in `evidence.theme`. The platform already "
            "groups by subsystem area and by feature area EXACTLY, from the "
            "file paths recorded on each item's findings — so a theme epic "
            "earns its review only where those are blind: items carrying no "
            "findings or no recorded paths, and themes that cut across two "
            "area keys."
        ),
    ),
    EpicAxis.CO_CHANGE.value: _Goal(
        axis=EpicAxis.CO_CHANGE,
        block=(
            "**CO-CHANGE** — unrelated problems whose fixes land in the same "
            "code. Nothing connects them conceptually; one pull request is "
            "simply cheaper than three, and separate branches would conflict "
            "on the same lines. Stamp `axis='co-change'` and list the code "
            "they will collide on in `evidence.shared_paths`. The platform "
            "already groups items whose RECORDED paths overlap; yours is the "
            "prediction — where the fix will actually land — for items whose "
            "paths are unrecorded, or whose fix reaches beyond the file the "
            "symptom showed up in."
        ),
    ),
}

GROUPING_GOALS = frozenset(_GROUPING_GOALS)


def _resolve_goals(raw: Sequence[str]) -> list[_Goal]:
    """The requested goals, deduped and in a stable order.

    Unknown goals are dropped rather than raising — a saved rule carrying a
    goal from a later version should still dispatch a sane run. Dropping every
    goal would leave a prompt with no task at all, so the default stands in.
    """
    seen = [g for g in dict.fromkeys(raw) if g in _GROUPING_GOALS]
    if not seen:
        seen = [EpicAxis.ROOT_CAUSE.value]
    return [_GROUPING_GOALS[g] for g in seen]


# How hard to push for epics. The old prompt hardcoded the `conservative`
# stance ("a wrong grouping is worse than no grouping", "at most 4") and then
# people wondered why so few groups came back — the answer was in the prompt,
# not the model. Now it is a dial, and the default admits that a rejected
# proposal costs one click while a missed epic costs a whole extra PR.
_GROUPING_STANCES: dict[str, str] = {
    "conservative": (
        "Propose an epic only when the shared cause is explicit in the code. "
        "A wrong grouping is worse than no grouping. At most 3 epics of 2-6 "
        "tickets; leave anything doubtful ungrouped."
    ),
    "balanced": (
        "Propose an epic when the shared cause is supported by code you have "
        "actually read. At most 6 epics of 2-6 tickets. A human rejects a "
        "wrong epic in one click, so a plausible epic you can evidence is "
        "worth proposing — but a guess you cannot point at code for is not."
    ),
    "exhaustive": (
        "Find every shared cause you can evidence, including ones spanning "
        "distant parts of the tree. At most 10 epics of 2-8 tickets. Prefer "
        "proposing a supportable epic over leaving tickets unexamined; the "
        "human review gate is the filter, not your caution."
    ),
}

GROUPING_AGGRESSIVENESS = frozenset(_GROUPING_STANCES)


#: Computed axes worth showing the agent as prior art. `lens` is excluded
#: because it spreads across the whole repository by design and reads as a
#: cluster of everything; `linked` because its label is a finding uid, which
#: tells a reader nothing.
_SEED_AXES = (EpicAxis.AREA, EpicAxis.FEATURE, EpicAxis.FILES, EpicAxis.CLASS)

#: How many computed clusters the prompt carries. Strongest first, so the cap
#: drops the marginal ones rather than a random slice.
_MAX_SEED_CLUSTERS = 12

#: Paths listed per candidate before eliding. The agent reads the code itself;
#: this is orientation, not a manifest.
_PATH_SAMPLE = 3


def computed_clusters(facts: list[TicketFacts]) -> list[EpicDraft]:
    """The groupings arithmetic already sees over this selection.

    These are NOT persisted. A grouping run that quietly created rule
    proposals as a side effect would put epics in the review queue that nobody
    asked for; they exist here only as prior art in the prompt, so the agent
    can extend and cut across them instead of re-deriving them by eye.

    Uncapped members (`max_members=0`) because a cluster split into "(1/3)"
    parts is an artifact of PR sizing, not of what the platform knows — and
    the agent is being shown what is known, not how it would ship.
    """
    drafts: list[EpicDraft] = []
    for axis in _SEED_AXES:
        drafts += partition(
            facts,
            EpicPartition(axis=axis, max_epics=0, min_members=2, max_members=0),
        )
    drafts.sort(key=lambda d: (-len(d.member_ticket_uids), d.title))
    return drafts[:_MAX_SEED_CLUSTERS]


def _candidate_block(facts: Sequence[TicketFacts]) -> str:
    """One entry per work item: what it is, plus every fact the computed axes
    keyed on. Facets a ticket does not carry are omitted rather than printed as
    "(none)" — and their absence is itself the signal that the computed axes
    could not place this item."""
    lines: list[str] = []
    for f in facts:
        desc = (f.description or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200] + "…"
        head = f"- uid: {f.uid}\n  title: {f.title}"
        meta = f"  status: {f.status} · priority: {f.priority}"
        if f.severity:
            meta += f" · severity: {f.severity}"
        if f.labels:
            meta += f" · labels: {', '.join(f.labels)}"
        entry = [head, meta]
        keyed: list[str] = []
        if f.area_keys:
            keyed.append(f"area: {', '.join(f.area_keys)}")
        if f.feature_keys:
            keyed.append(f"feature: {', '.join(f.feature_keys)}")
        if f.kind:
            keyed.append(f"lens: {f.kind}")
        if f.subtype and f.tags:
            keyed.append(f"class: {f.tags[0]}/{f.subtype}")
        if keyed:
            entry.append(f"  {' · '.join(keyed)}")
        if f.paths:
            shown = ", ".join(f.paths[:_PATH_SAMPLE])
            extra = len(f.paths) - _PATH_SAMPLE
            entry.append(f"  paths: {shown}{f' (+{extra})' if extra > 0 else ''}")
        else:
            # Said out loud because it is the whole reason a model is being
            # asked: no paths means no area key, which means every computed
            # cluster below is blind to this item.
            entry.append("  paths: none recorded — invisible to every computed axis")
        entry.append(f"  description: {desc or '(none)'}")
        lines.append("\n".join(entry))
    return "\n".join(lines)


def _clusters_block(clusters: Sequence[EpicDraft]) -> str:
    """The computed groupings, plus what the agent is allowed to do to them.

    Showing prior art is what stops the run re-proposing what "Build epics by
    rule" produces for free — the failure mode where a human reviews the same
    grouping twice, once from each producer.
    """
    if not clusters:
        return (
            "Groupings the platform already computed\n"
            "None: no two of the items below share an area, a feature, a "
            "recorded file, or a finding class. Everything here is yours to "
            "find.\n"
        )
    listing = "\n".join(
        f"- [{d.axis.value}] {d.title} — "
        f"{', '.join(d.member_ticket_uids)}"
        for d in clusters
    )
    return (
        "Groupings the platform already computed\n"
        "These come from set arithmetic over the items' recorded area keys, "
        "file paths and finding classes. A human can reproduce them at any "
        "time, without you, from “Build epics by rule” — so re-proposing one "
        "unchanged is a review someone pays for nothing.\n"
        f"{listing}\n"
        "\n"
        "Work from them. You may:\n"
        "* MERGE two of them that are really one job;\n"
        "* SPLIT one whose members turn out not to share a fix;\n"
        "* EXTEND one with items arithmetic could not key — an item with no "
        "recorded paths holds no area key and appears in no cluster above, no "
        "matter how plainly it belongs;\n"
        "* propose a group that CUTS ACROSS them.\n"
        "When your epic changes a cluster, put that cluster's label in "
        "`evidence.extends` so the reviewer sees the delta instead of a second "
        "copy. A cluster you would leave exactly as it is: say nothing about "
        "it, and do not propose it.\n"
    )


def _build_epic_proposal_intent(
    facts: list[TicketFacts],
    repository_uid: str,
    aggressiveness: str = "balanced",
    goals: Sequence[str] = (EpicAxis.ROOT_CAUSE.value,),
    clusters: Sequence[EpicDraft] = (),
) -> str:
    resolved = _resolve_goals(goals)
    stance = _GROUPING_STANCES.get(aggressiveness, _GROUPING_STANCES["balanced"])

    goal_blocks = "\n\n".join(f"{i}. {g.block}" for i, g in enumerate(resolved, 1))
    axes = ", ".join(f"'{g.axis.value}'" for g in resolved)

    # The prohibition is the ROOT-CAUSE guard rail, not a global law. It was
    # written when root cause was the only goal, and left in place it forbids
    # the very goals below it.
    guard_rail = ""
    if all(g.axis is EpicAxis.ROOT_CAUSE for g in resolved):
        guard_rail = (
            "Group ONLY on shared root cause. Do not group on shared files, "
            "shared subsystem, shared area, shared label, or shared theme: the "
            "platform already computes those groupings exactly, without a "
            "model, and an epic you propose on one of those axes is duplicated "
            "work that a human then has to review. Your judgment is wanted for "
            "the one thing arithmetic cannot see — that these "
            "different-looking work items are the same bug wearing different "
            "clothes.\n"
            "\n"
        )

    return (
        "Analyze the open work items below and propose which of them should be "
        "IMPLEMENTED TOGETHER — one implement run, one branch, one pull "
        "request. The implement agents are capable of fixing several related "
        "items in a single pass, so a good grouping turns n reviews and n "
        "branches into one. This is read-only against the repository — do not "
        "modify any code.\n"
        "\n"
        f"{guard_rail}"
        "What counts as belonging together — propose on these grounds and no "
        "others:\n"
        "\n"
        f"{goal_blocks}\n"
        "\n"
        f"Repository uid: {repository_uid}\n"
        "\n"
        f"{_clusters_block(clusters)}"
        "\n"
        "Candidate work items (not already in an epic):\n"
        f"{_candidate_block(facts)}\n"
        "\n"
        "Task:\n"
        "1. Read the code these items touch. A grouping you cannot point at "
        "code for is a guess, and the review gate is not there to catch "
        "guesses.\n"
        f"2. {stance} That ceiling is the total across every ground above, "
        "not a budget per ground.\n"
        "3. For each epic, call `opensweep_platform_propose_epic` with the "
        "repository_uid, a short `title`, `member_ticket_uids`, the `axis` "
        f"matching the ground you grouped on ({axes}), and `evidence` — a "
        "small object naming the fact and the code that proves it, e.g. "
        '{"root_cause": "callers must remember to await close()", '
        '"shared_paths": ["a/b.py", "c/d.py"]}. Optionally `suggested_labels` '
        "and `suggested_priority`.\n"
        f"4. Keep `rationale` to ONE sentence under {MAX_RATIONALE_CHARS} "
        "characters — it is rendered on a single line in the review list, and "
        "longer text is truncated, not shown. Put the specifics in "
        "`evidence`, which is rendered structurally.\n"
        "5. Never place one work item in two epics.\n"
        "Do not create tickets, do not change ticket statuses, and do not "
        "file findings. A human reviews every proposal: approval creates the "
        "parent ticket with your members as subtickets; rejection discards it."
    )


@router.post("/suggest-epics", operation_id="opensweep_ticket_suggest_epics")
async def suggest_epics(
    req: SuggestEpicsRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Dispatch a read-only run that analyzes ungrouped work items and
    proposes groupings via `opensweep_platform_propose_epic`. Every proposal is
    human-approved before anything changes.

    The run is seeded with the groupings the deterministic axes already see, so
    it spends its judgment on what they cannot — extending a cluster with items
    that carry no paths, cutting across two of them, or naming a shared cause
    behind items that look unrelated.
    """
    from domains.run_policies.services.effort import ensure_policy_for_effort
    from domains.runs.schemas import Effort, RunTrigger
    from domains.runs.services.lifecycle import LifecycleError, trigger_run
    from domains.tickets.services.epics.loader import load_ticket_facts
    from infrastructure.audit import write_audit

    await require_repo_in_org(req.repository_uid, user.org_uid)
    # `load_ticket_facts` already drops grouped tickets and their parents, and
    # resolves the finding-derived facets the computed axes key on — the same
    # pool the rule builder plans over, so the two producers cannot disagree
    # about what is eligible.
    facts = await load_ticket_facts(req.repository_uid)
    candidates = select_tickets(
        facts,
        EpicSelection(
            statuses=tuple(req.statuses),
            min_priority=req.min_priority,
            min_severity=req.min_severity,
            labels=tuple(req.labels),
            kinds=tuple(req.kinds),
            area_keys=tuple(req.area_keys),
            limit=req.limit,
            sort=req.sort,
        ),
    )
    if len(candidates) < 2:
        raise HTTPException(
            status_code=409,
            detail=(
                "need at least 2 ungrouped work items matching these filters "
                f"to propose groups — {len(facts)} candidate(s) exist, "
                f"{len(candidates)} passed"
            ),
        )
    goals = [g.axis.value for g in _resolve_goals(req.goals)]
    clusters = computed_clusters(candidates)

    # Specialized refine run: the grouping template IS the instructions
    # (custom_intent), so a replace overlay never displaces it; org append
    # guidance and the framing header/footer still stack.
    from domains.agents.services.composition import compose_agent_intent

    composed = await compose_agent_intent(
        repository_uid=req.repository_uid,
        agent_key="refine",
        stage="refine",
        repo_guidance="",
        custom_intent=_build_epic_proposal_intent(
            candidates,
            req.repository_uid,
            req.aggressiveness,
            goals,
            clusters,
        ),
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(Effort.NORMAL)
    # One grouping run = one reviewable plan. `plan-epics` groups its output
    # under a shared plan_uid so a rule's epics cost ONE approval click; agent
    # proposals defaulted to "" and so landed in the ungrouped bucket, costing
    # one click each — the exact tedium the bulk gate exists to remove. Minted
    # here and carried on the run, because the agent must not be trusted to
    # echo it back and the platform tool can read it off the run it already
    # resolves from the X-OpenSweep-Run-Uid header.
    plan_uid = uuid4().hex
    await write_audit(
        kind="epic.propose.requested",
        subject_uid=req.repository_uid,
        subject_type="Repository",
        actor_uid=user.uid,
        payload={
            "candidate_count": len(candidates),
            "goals": goals,
            "aggressiveness": req.aggressiveness,
            "seeded_clusters": len(clusters),
            "plan_uid": plan_uid,
        },
    )
    try:
        run = await trigger_run(
            repository_uid=req.repository_uid,
            intent=intent,
            playbook="refine",
            title="Propose ticket groups",
            target={
                "kind": "ticket-grouping",
                "plan_uid": plan_uid,
                # The clusters this run was shown as prior art. Kept so
                # `propose` can reject a verbatim re-proposal of a grouping the
                # platform already computed for free.
                "seeded_cluster_members": [
                    sorted(d.member_ticket_uids) for d in clusters
                ],
            },
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "plan_uid": plan_uid,
        "scheduled_agent_uid": run.scheduled_agent_uid,
        "candidate_count": len(candidates),
        "goals": goals,
        # What the run was shown as prior art. A caller seeing 0 here knows the
        # agent is working from scratch, not that it ignored the clusters.
        "seeded_cluster_count": len(clusters),
    }


@router.post("/plan-epics", operation_id="opensweep_ticket_plan_epics")
async def build_ticket_epics(
    req: PlanEpicsRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Select tickets by rule and cut them into epics on a COMPUTED axis —
    no agent, no run, answers immediately.

    This is the "top X issues in Y runs" path. Six of the nine proposable axes
    (area, feature, files, lens, class, linked) are arithmetic over data the
    tickets already carry, so asking a model to eyeball them only added latency
    and variance. `root-cause`, `theme` and `co-change` are the exceptions and
    still go through the agent
    proposal tool (`opensweep_platform_propose_epic`).

    `dry_run` returns the identical plan without persisting, so the counts a
    reviewer approves against come from the code that builds them.
    """
    from domains.tickets.services.epics.builder import plan_epics

    await require_repo_in_org(req.repository_uid, user.org_uid)
    return await plan_epics(req, actor_uid=user.uid)


class BulkTicketsRequest(BaseModel):
    uids: list[str] = Field(min_length=1, max_length=200)


class BulkTransitionRequest(BulkTicketsRequest):
    status: TicketStatus


async def _bulk(
    uids: Sequence[str],
    org_uid: str,
    action: Callable[..., Awaitable[object]],
) -> dict:
    """Apply `action` to each ticket, reporting partial success.

    Mirrors `EpicService.bulk_approve`, deliberately and in both respects:

    * Tenancy is checked PER TICKET, before that ticket is touched. The uid
      list is client-supplied and could otherwise mix in another org's ticket.
    * A failure is REPORTED, not raised. Approving 30 tickets where one is
      archived should land the other 29 and name the one that didn't — raising
      on the first would make the outcome depend on list order.
    """
    # Local import to match this module's convention (the lifecycle package
    # pulls in the executor registry). CapacityExceededError is a subclass, so
    # a provider at its ceiling lands here as a per-ticket error rather than
    # escaping as a 500 and discarding the tickets that DID dispatch.
    from domains.runs.services.lifecycle import LifecycleError

    service = TicketService()
    ok: list[str] = []
    errors: list[dict[str, str]] = []
    for uid in dict.fromkeys(uids):
        try:
            ticket = await service.get_node(uid)
            await require_repo_in_org(ticket.repository_uid, org_uid)
            await action(ticket)
            ok.append(uid)
        except HTTPException as exc:
            errors.append({"uid": uid, "detail": str(exc.detail)})
        except LifecycleError as exc:
            errors.append({"uid": uid, "detail": str(exc)})
    return {"ok": ok, "errors": errors}


@router.post("/bulk-status", operation_id="opensweep_ticket_bulk_transition")
async def bulk_transition_tickets(
    req: BulkTransitionRequest, user: UserDTO = Depends(get_current_user)
) -> dict:
    """Move N tickets in one action — Gate 1 for a whole selection.

    Legality, audit records and the Gate-1 role check are inherited from
    `TicketService.transition`; this only fans out."""
    return await _bulk(
        req.uids,
        user.org_uid,
        lambda t: TicketService().transition(
            t.uid, req.status.value, actor_uid=user.uid, actor_role=user.role
        ),
    )


@router.post("/bulk-implement", operation_id="opensweep_ticket_bulk_implement")
async def bulk_implement_tickets(
    req: BulkTicketsRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Dispatch implement runs for N approved tickets.

    Each dispatch goes through the same `trigger_implement_run` as the single
    route, so the provider concurrency ceiling, the one-write-run-per-ticket
    guard and the predicted-path guard all apply per ticket. A selection larger
    than the provider's headroom therefore lands the first few and reports the
    rest as refused — it does NOT stampede the provider, which is the whole
    reason the capacity gate had to exist before this endpoint did.
    """
    from domains.delivery.services.implement_run_service import trigger_implement_run

    dispatched: dict[str, str] = {}

    async def _go(ticket) -> None:
        run = await trigger_implement_run(ticket, triggered_by=user.uid)
        dispatched[ticket.uid] = run.uid

    result = await _bulk(req.uids, user.org_uid, _go)
    result["runs"] = dispatched
    return result


@router.post("/bulk-archive", operation_id="opensweep_ticket_bulk_archive")
async def bulk_archive_tickets(
    req: BulkTicketsRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Archive N tickets — the reversible 'clear the board' action."""
    return await _bulk(
        req.uids,
        user.org_uid,
        lambda t: TicketService().archive(t.uid, actor_uid=user.uid),
    )


@router.post("/triage-batch", operation_id="opensweep_ticket_triage_batch")
async def triage_batch(
    req: TriageBatchRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Triage a SELECTION of tickets in one run — questions asked once, not N times.

    Not "refine-batch": that name reads as "dispatch N refine runs", which is
    exactly what this is not. One run reads the shared code once, classifies
    every question across the whole selection, answers the local ones itself
    into the assumption ledger, and asks only what genuinely spans tickets.
    """
    from domains.tickets.services.batch_triage import MAX_BATCH, dispatch_batch_triage

    await require_repo_in_org(req.repository_uid, user.org_uid)

    # `exclude_grouped=False` — a DELIBERATE divergence from both epic
    # endpoints. Grouping must not re-place a ticket already in an epic;
    # triage has the opposite need, because an epic's PR closes every member
    # unconditionally on merge, so a member with vague acceptance criteria is
    # MORE dangerous than a loose ticket, not less.
    facts = await load_ticket_facts(req.repository_uid, exclude_grouped=False)
    candidates = select_tickets(
        facts,
        EpicSelection(
            statuses=tuple(req.statuses),
            min_priority=req.min_priority,
            min_severity=req.min_severity,
            labels=tuple(req.labels),
            kinds=tuple(req.kinds),
            area_keys=tuple(req.area_keys),
            limit=req.limit,
            sort=req.sort,
        ),
    )
    if not candidates:
        raise HTTPException(
            status_code=409,
            detail=f"no tickets match these filters ({len(facts)} candidate(s) exist)",
        )
    if len(candidates) > MAX_BATCH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(candidates)} tickets selected; the batch ceiling is "
                f"{MAX_BATCH}. Context grows faster than it earns beyond that — "
                "narrow the filters or lower `limit`."
            ),
        )
    if req.dry_run:
        return {
            "dry_run": True,
            "selected": [c.uid for c in candidates],
            "count": len(candidates),
        }

    # Two overlapping batches would double-write the same tickets and
    # double-ask the same policy questions. `blocking_run` cannot catch it: a
    # batch is playbook="refine" with no linked_ticket_uid. Serializing on the
    # repository instead would 409 `suggest-epics`, which is the same playbook
    # on the same repo and entirely unrelated — so guard narrowly on the
    # target kind rather than widening `filter_active_runs` for one caller.
    from domains.runs.services.active_runs import active_runs_for, conflict_detail

    for run in await active_runs_for(repository_uid=req.repository_uid):
        if dict(run.target or {}).get("kind") == "ticket-triage":
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(
                    "a triage batch is already running for this repository", run
                ),
            )

    run = await dispatch_batch_triage(
        repository_uid=req.repository_uid,
        candidates=candidates,
        actor_uid=user.uid,
        org_uid=user.org_uid,
        autonomy=req.autonomy,
    )
    return {
        "run_uid": run.uid,
        "ticket_uids": [c.uid for c in candidates],
        "count": len(candidates),
        "effort": run.effort,
    }


@router.post("/{uid}/dissolve-epic", operation_id="opensweep_ticket_dissolve_epic")
async def ungroup_ticket(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Dissolve a group: detach every subticket from this parent. The parent
    ticket itself is kept."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    detached = await service.ungroup(uid, actor_uid=user.uid)
    return {"ticket_uid": uid, "detached": detached}


@router.post(
    "/{uid}/remove-from-epic",
    response_model=TicketDTO,
    operation_id="opensweep_ticket_remove_from_epic",
)
async def remove_ticket_from_group(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    """Detach this ticket from its parent group."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.remove_from_group(uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.get("/{uid}", response_model=TicketDetailDTO, operation_id="opensweep_ticket_get")
async def get_ticket(uid: str, user: UserDTO = Depends(get_current_user)):
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    return await service.get_detail(uid)


@router.post("", response_model=TicketDTO, operation_id="opensweep_ticket_create")
async def create_ticket(req: CreateTicketRequest, user: UserDTO = Depends(require_role("maintainer"))):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    t = await TicketService().create(req, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.patch("/{uid}", response_model=TicketDTO, operation_id="opensweep_ticket_update")
async def update_ticket(
    uid: str, req: UpdateTicketRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.update(uid, req, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/status", response_model=TicketDTO, operation_id="opensweep_ticket_transition")
async def transition_ticket(
    uid: str, req: TransitionTicketRequest, user: UserDTO = Depends(get_current_user)
):
    """Legality-checked move; backlog → todo is Gate 1 (maintainer+, audited
    as ticket.approved). Illegal transitions → 409."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.transition(
        uid, req.status.value, actor_uid=user.uid, actor_role=user.role
    )
    return ticket_to_dto(t)


@router.post(
    "/{uid}/link-finding", response_model=TicketDTO, operation_id="opensweep_ticket_link_finding"
)
async def link_finding(
    uid: str, req: LinkFindingRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    from domains.findings.models import Finding

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    finding = await Finding.nodes.get_or_none(uid=req.finding_uid)
    if finding is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_repo_in_org(finding.repository_uid, user.org_uid)
    if finding.repository_uid != ticket.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")
    t = await service.link_finding(uid, req.finding_uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/link-pr", response_model=TicketDTO, operation_id="opensweep_ticket_link_pr")
async def link_pr(
    uid: str, req: LinkPullRequestRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    from domains.delivery.models import PullRequest

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    pr = await PullRequest.nodes.get_or_none(uid=req.pull_request_uid)
    if pr is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    if pr.repository_uid != ticket.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")
    t = await service.link_pr(uid, req.pull_request_uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/implement", operation_id="opensweep_ticket_implement")
async def implement_ticket(
    uid: str,
    force: bool = Query(
        False,
        description=(
            "Dispatch even when another in-flight write run is predicted to "
            "touch the same files. Overrides ONLY the path heuristic — the "
            "one-write-run-per-ticket guard still applies."
        ),
    ),
    user: UserDTO = Depends(require_role("maintainer")),
) -> dict:
    """Dispatch a write-path implement run for a Gate-1-approved ticket (§6).

    409 when the ticket hasn't passed Gate 1 or an open PR already implements
    it; an existing remote branch is adopted, not duplicated. The agent
    commits in a write sandbox; the platform validates, pushes, and opens a
    draft PR."""
    from domains.delivery.services.implement_run_service import trigger_implement_run
    from domains.runs.services.lifecycle import LifecycleError

    ticket = await TicketService().get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    if bool(getattr(ticket, "archived", False)):
        raise HTTPException(status_code=409, detail="ticket is archived — unarchive it first")
    # A live thread owns this ticket's work branch — a parallel one-shot
    # implement run would race it on the branch and the fix-round ledger.
    from domains.threads.services.thread_service import ThreadService, has_active_thread

    if has_active_thread(await ThreadService().list(subject_ticket_uid=uid)):
        raise HTTPException(
            status_code=409,
            detail="this ticket has an active thread — approve implementation from the thread instead",
        )
    try:
        run = await trigger_implement_run(
            ticket, triggered_by=user.uid, force_path_conflict=force
        )
        if force:
            await write_audit(
                kind="implement_run.path_conflict_overridden",
                subject_uid=ticket.uid,
                subject_type="Ticket",
                actor_uid=user.uid,
                repository_uid=ticket.repository_uid,
                payload={"run_uid": run.uid},
            )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "scheduled_agent_uid": run.scheduled_agent_uid,
        "ticket_uid": uid,
    }


@router.post("/{uid}/refine", operation_id="opensweep_ticket_refine")
async def refine_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    """Dispatch a read-only refine run that enriches the ticket in place —
    sharpening its title, description and acceptance criteria and attaching an
    implementation plan + relevant files via the platform tools."""
    from domains.tickets.services.refine_dispatch import dispatch_refine_run

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    if bool(getattr(ticket, "archived", False)):
        raise HTTPException(status_code=409, detail="ticket is archived — unarchive it first")
    run = await dispatch_refine_run(ticket, actor_uid=user.uid, org_uid=user.org_uid)
    return {"run_uid": run.uid, "scheduled_agent_uid": run.scheduled_agent_uid, "ticket_uid": uid}


@router.post(
    "/{uid}/archive", response_model=TicketDTO, operation_id="opensweep_ticket_archive"
)
async def archive_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Reversible "leave the board" from any status — the alternative to
    delete, which stays backlog-only. 409 while an active thread owns the
    ticket, for epic members (archive the parent) and for epics with
    unfinished members."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    return ticket_to_dto(await service.archive(uid, actor_uid=user.uid))


@router.post(
    "/{uid}/unarchive", response_model=TicketDTO, operation_id="opensweep_ticket_unarchive"
)
async def unarchive_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Restore an archived ticket to its lane (status was never touched)."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    return ticket_to_dto(await service.unarchive(uid, actor_uid=user.uid))


@router.delete("/{uid}", status_code=204, operation_id="opensweep_ticket_delete")
async def delete_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Backlog-only deletable — anything approved keeps its history (409 otherwise)."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    await service.delete(uid, actor_uid=user.uid)
    return Response(status_code=204)
