"""Implement-run — write-path run turning an approved Ticket into a draft PR
(PLATFORM_V2_DESIGN.md §6, §15 Phase 3).

Flow (agent writes, PLATFORM pushes — never the other way around):

    Ticket (todo/in-progress, Gate 1 passed)
      → write sandbox (fresh GitHub clone, work branch checked out)
      → implement run (ExecutionMode.IMPLEMENT; agent edits, tests, commits;
        it never sees the GITHUB_TOKEN and is told not to push)
      → post-run finalize (deterministic platform code):
          write_gate.validate_sandbox_changes
            ok         → push branch, open DRAFT PR, link ticket → in-review
            violations → NO push; sandbox retained for inspection; audited

Idempotency: an existing open PR for the ticket → 409 pointer; an existing
remote branch is ADOPTED (checkout_existing continuation), never duplicated.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from domains.delivery.models import PullRequest
from domains.delivery.services import write_gate
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.delivery.services.run_dispatch import (
    DOC_UPKEEP_INTENT_SECTION,
    dispatch_serialized,
    finalize_write_run,
    require_repository,
)
from domains.docs.services.doc_freshness import docs_watching_paths
from domains.execution.schemas import SandboxDTO
from domains.execution.services.sandbox_service import SandboxService
from domains.findings.models import Finding
from domains.repositories.models import Repository
from domains.repositories.services.repository_service import repository_to_dto
from domains.repositories.services.workflow import stage_prompt_body
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.runs.models import Run
from domains.runs.schemas import (
    Effort,
    ExecutionMode,
    RunTrigger,
)
from domains.runs.services.lifecycle import trigger_run
from domains.tickets.models import Ticket
from infrastructure.audit import write_audit
from infrastructure.git_providers import get_provider_client

IMPLEMENTABLE_TICKET_STATUSES = {"todo", "in-progress"}


def slug(text: str, max_len: int = 30) -> str:
    """Branch-safe slug: lowercase, [a-z0-9-], collapsed, length-capped."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "work"


def branch_name_for_ticket(ticket: Ticket) -> str:
    return f"opensweep/{ticket.uid[:8]}-{slug(ticket.title or '', 30)}"


def build_implement_intent(
    ticket: Ticket,
    *,
    work_branch: str,
    base_branch: str,
    denylist: list[str],
    continuation: bool = False,
    addendum: str = "",
) -> str:
    criteria = [str(c) for c in (ticket.acceptance_criteria or []) if str(c).strip()]
    ac_block = (
        "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
        or "(none recorded — implement the described change minimally and say so in your summary)"
    )
    deny_block = "\n".join(f"   - `{p}`" for p in denylist) or "   - (none configured)"
    continuation_note = (
        " It already contains earlier work for this ticket — continue from it, do not restart."
        if continuation
        else ""
    )
    description = (ticket.description or "").strip() or "(no further description)"
    intent = (
        f"Implement ticket \"{ticket.title}\" (`{ticket.uid}`) in this working copy.\n"
        "\n"
        "## Ticket\n"
        f"{description}\n"
        "\n"
        "## Acceptance criteria (implement these, minimally)\n"
        f"{ac_block}\n"
        "\n"
        "## Working copy\n"
        f"- The work branch `{work_branch}` is already checked out in your current directory.{continuation_note}\n"
        f"- Base branch: `{base_branch}`. Never switch branches.\n"
        "\n"
        "## Rules (the platform validates your commits after the run)\n"
        "- Make the MINIMAL change that satisfies the acceptance criteria — no drive-by refactors.\n"
        "- Do NOT touch any path matching these forbidden patterns (the platform blocks the push otherwise):\n"
        f"{deny_block}\n"
        f"- Commit with conventional commit message(s) referencing `OpenSweep-Ticket: {ticket.uid}`.\n"
        "- DO NOT push. Never run `git push` — the platform validates and pushes your branch.\n"
        "\n"
        "## Tests\n"
        "- Discover and run the repository's test suites where feasible: a `pyproject.toml` with\n"
        "  pytest configured → run pytest; a `package.json` with test scripts → run them.\n"
        "  Make them pass for your change; report failures honestly.\n"
        "\n"
        "## How to test this change (definition of done)\n"
        "- Attach a TEST NOTE via `attach_artifact` (target_type `ticket`, target_uid\n"
        f"  `{ticket.uid}`, artifact_type `test_note`): the concrete manual verification\n"
        "  steps a human should follow on this branch — what to start, what to click or\n"
        "  call, and the expected behavior. Write it for someone who did not read the diff.\n"
        "- If the change only shows with data, commit seed data/fixtures on this branch\n"
        "  (extend the repo's existing seed script or test fixtures) and reference them\n"
        "  in the test note.\n"
        "- If you add or change migrations or environment setup, say so in the test note\n"
        "  (including how to reset), and update the repository's setup/testing\n"
        "  documentation page if one exists.\n"
        "\n"
        + DOC_UPKEEP_INTENT_SECTION
        + "\n"
        "## Finish (mandatory)\n"
        "- Call `complete_run` with a summary listing every commit you made (sha + message),\n"
        "  the test results (suites run, pass/fail), and the doc/memory upkeep you did\n"
        "  (pages updated/confirmed/proposed, memories written).\n"
    )
    if addendum.strip():
        intent += f"\n{addendum.strip()}\n"
    return intent


async def trigger_implement_run(
    ticket: Ticket,
    *,
    triggered_by: str = "",
    trigger: RunTrigger = RunTrigger.MANUAL,
    intent_addendum: str = "",
    force_path_conflict: bool = False,
) -> Run:
    """Create the write sandbox and dispatch the implement run.

    `force_path_conflict` overrides the predicted-path guard only (never the
    in-flight guard). The prediction is a heuristic — see
    `predicted_paths_for_ticket` — so a human must always be able to say "I
    know, do it anyway"."""
    # An epic member's work ships inside its PARENT's single run and PR, so
    # dispatching against the member directly would open a second branch over
    # the same files and race the epic's PR to merge — whichever merged first
    # would close the ticket while the other kept building against it. Gate 1
    # already refuses to approve a member, but a ticket can be grouped *after*
    # it was approved (approval deliberately leaves member statuses alone), so
    # a member sitting in `todo` is the ordinary case and the dispatch path has
    # to check for itself. First, so it costs nothing and cannot be masked by a
    # repository or status error.
    if ticket.parent_ticket_uid:
        raise HTTPException(
            status_code=409,
            detail=(
                "this ticket belongs to an epic — implement the epic parent "
                f"({ticket.parent_ticket_uid}), whose run covers every subticket. "
                "Remove it from the epic to work it on its own."
            ),
        )

    # A group parent's real work is its subtickets, so every dispatch path
    # must carry them. This lives here rather than in the callers because it
    # was previously added only by the two Thread paths: a one-shot
    # `POST /tickets/{uid}/implement` on a group parent produced a run that
    # implemented the parent's own (usually near-empty) description while
    # `mark_done_via_merge` still closed every child on merge — an epic
    # silently shipping as a no-op.
    from domains.threads.services.intents import build_epic_addendum
    from domains.tickets.models import Ticket as _Ticket

    children = list(await _Ticket.nodes.filter(parent_ticket_uid=ticket.uid))
    if children:
        intent_addendum = (intent_addendum or "") + build_epic_addendum(children)

    if (ticket.status or "") not in IMPLEMENTABLE_TICKET_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"ticket must have passed Gate 1 (status todo or in-progress) — it is "
                f"'{ticket.status}'"
            ),
        )
    repo = await require_repository(ticket.repository_uid, require_github=True)

    # Ask whether we may push BEFORE spending a run — cloning only needs read,
    # so a read-only credential otherwise gets discovered at delivery time with
    # the work already done. Silent when the answer is unclear.
    denial = await write_gate.write_access_denial_reason(repo)
    if denial:
        raise HTTPException(status_code=409, detail=denial)

    async def _dispatch() -> Run:
        # Idempotency 1: an open PR already implementing this ticket → point at it.
        existing_prs = await PullRequest.nodes.filter(ticket_uid=ticket.uid)
        open_pr = next((p for p in existing_prs if p.state == "open"), None)
        if open_pr is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"an open pull request already implements this ticket: "
                    f"PR #{open_pr.github_number} (uid={open_pr.uid}) {open_pr.url or ''}".strip()
                ),
            )

        work_branch = branch_name_for_ticket(ticket)
        base_branch = repo.default_branch or "main"

        # Idempotency 2: branch already on GitHub → adopt it (fix-style
        # continuation on the existing branch) instead of failing.
        checkout_existing = False
        client = get_provider_client(repo)
        if client.is_active:
            branch = await client.get_branch(repo.github_owner, repo.github_repo, work_branch)
            checkout_existing = branch is not None

        # Gate-1 follow-through: work is starting → in-progress (system, audited).
        # Rolled back below if the dispatch never actually starts — a failed
        # dispatch must not leave the board claiming work is underway.
        advanced_from_todo = False
        if ticket.status == "todo":
            ticket.status = "in-progress"
            ticket.updated_at = datetime.now(UTC)
            await ticket.save()
            advanced_from_todo = True
            await write_audit(
                kind="ticket.transitioned",
                subject_uid=ticket.uid,
                subject_type="Ticket",
                actor_uid="system",
                payload={"from": "todo", "to": "in-progress", "cause": "implement_run"},
            )

        try:
            return await _dispatch_implement(
                ticket=ticket,
                repo=repo,
                work_branch=work_branch,
                base_branch=base_branch,
                checkout_existing=checkout_existing,
                intent_addendum=intent_addendum,
                trigger=trigger,
                triggered_by=triggered_by,
                # Closure over `predicted`, assigned below before this runs —
                # the run stamps the same claim the guard just checked, so a
                # later dispatch sees exactly what was cleared.
                predicted_paths=predicted,
            )
        except Exception:
            if advanced_from_todo:
                ticket.status = "todo"
                ticket.updated_at = datetime.now(UTC)
                await ticket.save()
                await write_audit(
                    kind="ticket.transitioned",
                    subject_uid=ticket.uid,
                    subject_type="Ticket",
                    actor_uid="system",
                    payload={"from": "in-progress", "to": "todo", "cause": "implement_dispatch_failed"},
                )
            raise

    # In-flight guard: one WRITE run per ticket at a time — a second write run
    # would fight the first over the same work branch. Read-only runs (chat,
    # review) never block an implement dispatch. Serialized per ticket so two
    # concurrent dispatches can't both pass the guard.
    # Predicted-path guard: two DIFFERENT epics whose fixes land in the same
    # files would otherwise dispatch two runs on two branches with nothing
    # comparing them, and collide at merge. The per-ticket lock above cannot
    # see that, so this runs under a repository-scoped lock nested inside it.
    predicted = frozenset() if force_path_conflict else await predicted_paths_for_ticket(ticket)

    async def _guard() -> None:
        await assert_no_path_conflict(
            repository_uid=ticket.repository_uid,
            ticket_uid=ticket.uid,
            paths=predicted,
        )

    return await dispatch_serialized(
        target_uid=ticket.uid,
        playbook="implement",
        conflict_message="a write run is already in progress for this ticket",
        active_filter={"ticket_uid": ticket.uid},
        dispatch=_dispatch,
        scope_lock_key=f"implement-paths:{ticket.repository_uid}",
        scope_guard=_guard if predicted else None,
    )


async def _dispatch_implement(
    *,
    ticket,
    repo,
    work_branch: str,
    base_branch: str,
    checkout_existing: bool,
    intent_addendum: str,
    trigger: RunTrigger,
    triggered_by: str,
    predicted_paths: frozenset[str] = frozenset(),
):
    policy = await ensure_merge_policy(repo.uid)
    denylist = write_gate.effective_denylist(policy)
    run_policy = await ensure_policy_for_effort(Effort.NORMAL)

    await write_audit(
        kind="implement_run.requested",
        subject_uid=ticket.uid,
        subject_type="Ticket",
        actor_uid=triggered_by,
        payload={
            "work_branch": work_branch,
            "adopted_existing_branch": checkout_existing,
        },
    )

    # The slow git clone is deferred into the run's background pipeline via
    # this factory — the dispatch request returns as soon as the queued run
    # row exists, so the UI flips to "in progress" immediately. A prep failure
    # marks the run failed with usage["prep_failed"] (the agent never ran);
    # the finalizer skips those, so no local cleanup is needed here.
    repo_dto = repository_to_dto(repo)

    async def _make_sandbox() -> SandboxDTO:
        return await SandboxService().create_for_write(
            repository=repo_dto,
            agent_run_uid=ticket.uid,
            work_branch=work_branch,
            base_branch=base_branch,
            checkout_existing=checkout_existing,
        )

    # Org-agent-overlays composition: header + platform implement
    # instructions (org overlay applied) + repo implement guidance stack
    # around the structural implement contract (ticket, criteria, denylist).
    from domains.agents.services.composition import compose_agent_intent

    guidance = await stage_prompt_body(repo.uid, "implement")
    # Pre-load the pages documenting the code this ticket's findings touch, so
    # the briefing inlines them verbatim rather than making the agent fetch.
    target_doc_uids = await _docs_for_ticket(ticket)
    composed = await compose_agent_intent(
        repository_uid=repo.uid,
        agent_key="implement",
        stage="implement",
        repo_guidance=guidance or "",
        structural=build_implement_intent(
            ticket,
            work_branch=work_branch,
            base_branch=base_branch,
            denylist=denylist,
            continuation=checkout_existing,
            addendum=intent_addendum or "",
        ),
    )
    return await trigger_run(
        repository_uid=repo.uid,
        intent=composed.text,
        playbook="implement",
        title=f"Implement: {(ticket.title or '')[:70]}",
        target={
            "ticket_uid": ticket.uid,
            "work_branch": work_branch,
            "base_branch": base_branch,
            "continuation": checkout_existing,
            "doc_uids": target_doc_uids,
            # This run's claim on the tree. Sorted so the stored value is
            # reproducible; read back by `assert_no_path_conflict` when a LATER
            # dispatch asks whether it would collide with this one.
            "predicted_paths": sorted(predicted_paths),
        },
        linked_ticket_uid=ticket.uid,
        # executor=None: resolve the write-capable executor from the repo's
        # provider (claude_code or opencode) — the local-LLM delivery loop.
        executor=None,
        execution_mode=ExecutionMode.IMPLEMENT,
        run_policy_uid=run_policy.uid,
        effort=Effort.NORMAL.value,
        trigger=trigger,
        triggered_by=triggered_by,
        sandbox_factory=_make_sandbox,
    )


async def assert_no_path_conflict(
    *, repository_uid: str, ticket_uid: str, paths: frozenset[str]
) -> None:
    """409 when an in-flight write run has already claimed any of `paths`.

    Claims are read off `Run.target["predicted_paths"]`, stamped at dispatch.
    Runs that predate the stamp carry no claim and simply never block — the
    guard degrades to today's behaviour rather than to a false positive.

    Must be called under the repository scope lock (see `dispatch_serialized`),
    or two dispatches can both read "no conflict" before either stamps its own
    claim.
    """
    from domains.runs.services.active_runs import active_runs_for, conflict_detail
    from domains.runs.services.playbooks import WRITE_PLAYBOOKS
    from domains.tickets.services.epics.conflicts import (
        PathClaim,
        conflicting_claim,
        overlap,
    )

    if not paths:
        return
    claims: list[PathClaim] = []
    runs_by_uid = {}
    for run in await active_runs_for(repository_uid=repository_uid):
        if (run.playbook or "") not in WRITE_PLAYBOOKS:
            continue
        if (run.linked_ticket_uid or "") == ticket_uid:
            continue  # same ticket — the in-flight guard already ruled on it
        claimed = frozenset(dict(run.target or {}).get("predicted_paths") or [])
        if not claimed:
            continue
        runs_by_uid[run.uid] = run
        claims.append(
            PathClaim(
                run_uid=run.uid,
                ticket_uid=run.linked_ticket_uid or "",
                started_at=(run.started_at.isoformat() if run.started_at else ""),
                paths=claimed,
            )
        )

    hit = conflicting_claim(claims, paths=paths)
    if hit is None:
        return
    shared = overlap(hit.paths, paths)
    raise HTTPException(
        status_code=409,
        detail=conflict_detail(
            "another write run is already changing files this ticket needs: "
            + ", ".join(shared),
            runs_by_uid[hit.run_uid],
        )
        | {"overlapping_paths": list(shared), "ticket_uid": hit.ticket_uid},
    )


async def _raw_paths_for_ticket(ticket: Ticket) -> list[str]:
    """Un-normalized affected paths for this ticket AND its subtickets.

    A ticket carries no paths itself; its findings (origin + linked) do. The
    children union is not optional: an epic parent usually carries no findings
    of its own — its work IS the subtickets, which is why `build_epic_addendum`
    exists — so a parent-only read returns nothing at all. That made doc
    pre-load silently empty for every epic, and would make the path-conflict
    guard blind to exactly the dispatches it exists to catch.
    """
    from domains.tickets.models import Ticket as _Ticket

    subjects = [ticket, *await _Ticket.nodes.filter(parent_ticket_uid=ticket.uid)]
    finding_uids: list[str] = []
    for subject in subjects:
        finding_uids.extend(subject.linked_finding_uids or [])
        if subject.origin_finding_uid:
            finding_uids.append(subject.origin_finding_uid)

    paths: list[str] = []
    for fu in dict.fromkeys(finding_uids):
        f = await Finding.nodes.get_or_none(uid=fu)
        if f:
            paths.extend(f.affected_paths or [])
    return paths


async def predicted_paths_for_ticket(ticket: Ticket) -> frozenset[str]:
    """Files this ticket's implement run is expected to touch.

    Normalized through `epics.loader.normalize_path`, so `foo.py:12-34` and
    `foo.py` compare as ONE file — the same normalization the `files` epic axis
    depends on. A heuristic by construction: line anchors are stripped
    deliberately, so two tickets touching unrelated regions of one large file
    do collide. Callers must offer an override.

    Best-effort: on failure returns the empty set, which every caller treats as
    "no prediction" and therefore never blocks.
    """
    from domains.tickets.services.epics.loader import normalize_path

    try:
        raw = await _raw_paths_for_ticket(ticket)
    except Exception as exc:  # noqa: BLE001
        from logging_config import logger

        logger.warning(
            f"path prediction for ticket {ticket.uid} failed: {exc}",
            extra={"tag": "delivery"},
        )
        return frozenset()
    return frozenset(p for p in (normalize_path(x) for x in raw) if p)


async def _docs_for_ticket(ticket: Ticket) -> list[str]:
    """Doc uids watching the paths this ticket's findings touch — for briefing
    pre-load. Best-effort: any failure yields no pre-load (the briefing
    index + read_doc still cover every page).

    Shares `_raw_paths_for_ticket` with the conflict guard so the two path
    walks cannot drift — and so epics stop pre-loading nothing."""
    try:
        return await docs_watching_paths(
            ticket.repository_uid, await _raw_paths_for_ticket(ticket)
        )
    except Exception as exc:  # noqa: BLE001
        from logging_config import logger

        logger.warning(
            f"doc pre-load for ticket {ticket.uid} failed: {exc}",
            extra={"tag": "delivery"},
        )
        return []


async def finalize_implement_run(run: Run, *, quiet_when_unchanged: bool = False) -> None:
    """Per-turn playbook hook (V3 §3): validate → push → draft PR, or block +
    retain. Derived entirely from the run so it re-fires on follow-up turns
    (the draft-PR step is idempotent — an existing PR is adopted).

    Thread runs (unified dev flow rev2) reuse this per turn once their thread
    leaves the refining phase — with quiet_when_unchanged so conversational
    turns don't audit as blocked."""
    ticket_uid = run.linked_ticket_uid or str((run.target or {}).get("ticket_uid") or "")
    if not ticket_uid:
        return
    target = dict(run.target or {})
    work_branch = str(target.get("work_branch") or "")
    base_branch = str(target.get("base_branch") or "main")
    repository_uid = run.repository_uid
    if dict(run.usage or {}).get("prep_failed"):
        return

    async def _after_push(sandbox: SandboxDTO, result: write_gate.WriteGateResult) -> None:
        pr_uid = await open_draft_pr_for_ticket(
            repository_uid=repository_uid,
            ticket_uid=ticket_uid,
            work_branch=work_branch,
            base_branch=base_branch,
            run_uid=run.uid,
        )
        await write_audit(
            kind="implement_run.pr_opened",
            subject_uid=pr_uid or ticket_uid,
            subject_type="PullRequest" if pr_uid else "Ticket",
            actor_uid="system",
            payload={
                "ticket_uid": ticket_uid,
                "run_uid": run.uid,
                "work_branch": work_branch,
                "pull_request_uid": pr_uid,
            },
        )
        if pr_uid and not run.linked_pr_uid:
            run.linked_pr_uid = pr_uid
            await run.save()

        # Thread follow-through: a draft PR moves the run's thread (if any)
        # to in_review. Never raises.
        from domains.threads.services.hooks import note_pr_opened_for_run

        await note_pr_opened_for_run(run)

    await finalize_write_run(
        run,
        audit_prefix="implement_run",
        subject_uid=ticket_uid,
        subject_type="Ticket",
        repository_uid=repository_uid,
        base_ref=base_branch,
        work_branch=work_branch,
        on_pushed=_after_push,
        quiet_when_unchanged=quiet_when_unchanged,
    )


async def open_draft_pr_for_ticket(
    *,
    repository_uid: str,
    ticket_uid: str,
    work_branch: str,
    base_branch: str,
    run_uid: str = "",
) -> str:
    """Open (or adopt) the draft PR for a pushed work branch; link the ticket.

    Returns the PullRequest node uid ("" only if everything GitHub-side
    failed, which is surfaced via logs/audit rather than an exception —
    the branch push already succeeded and must not be rolled back).
    """
    # Local imports: pull_request_service ↔ delivery services would otherwise
    # form an import cycle through this module's service siblings.
    from domains.delivery.services.pull_request_service import PullRequestService
    from domains.tickets.services.ticket_service import TicketService

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    ticket = await Ticket.nodes.get_or_none(uid=ticket_uid)
    if repo is None or ticket is None:
        return ""
    client = get_provider_client(repo)
    if not client.is_active:
        return ""

    criteria = [str(c) for c in (ticket.acceptance_criteria or []) if str(c).strip()]
    ac_block = "\n".join(f"- [ ] {c}" for c in criteria) or "- [ ] (no acceptance criteria recorded)"
    # Rendered ONLY when non-empty, so a PR from an `interrogate` run is
    # byte-identical to what this produced before the dial existed.
    from domains.threads.services.intents import render_assumptions_md

    assumption_block = render_assumptions_md(ticket.assumptions or [])
    body = (
        f"OpenSweep-Ticket: {ticket.uid}\n"
        "\n"
        f"{(ticket.description or '').strip()}\n"
        "\n"
        "## Acceptance criteria\n"
        f"{ac_block}\n"
        f"{assumption_block}"
        "\n"
        f"_Opened by a OpenSweep implement-run{f' (run `{run_uid}`)' if run_uid else ''}. "
        "The agent committed in a sandbox; the platform validated and pushed._\n"
    )

    try:
        payload = await client.open_pull_request(
            repo.github_owner,
            repo.github_repo,
            head=work_branch,
            base=base_branch,
            title=ticket.title or f"OpenSweep: {work_branch}",
            body=body,
            draft=True,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422:
            raise
        # 422: a PR for this head already exists — adopt it (idempotency).
        prs = await client.list_pull_requests(repo.github_owner, repo.github_repo, state="open")
        payload = next(
            (p for p in prs if ((p.get("head") or {}).get("ref")) == work_branch), None
        )
        if payload is None:
            raise

    service = PullRequestService()
    pr = await service.upsert_from_payload(repo, payload)
    pr.ticket_uid = ticket.uid
    pr.updated_at = datetime.now(UTC)
    await pr.save()
    # link_pr auto-advances the ticket todo/in-progress → in-review (system).
    await TicketService().link_pr(ticket.uid, pr.uid, actor_uid="system")
    return pr.uid
