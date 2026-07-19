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
from domains.runs.models import Run
from domains.runs.schemas import (
    ExecutionMode,
    Executor,
    Effort,
    RunTrigger,
)
from domains.runs.services.lifecycle import trigger_run
from domains.repositories.models import Repository
from domains.repositories.services.repository_service import repository_to_dto
from domains.repositories.services.workflow import stage_prompt_body
from domains.run_policies.services.effort import ensure_policy_for_effort
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


def build_ratchet_addendum(tag: str, subtype: str, findings: list) -> str:
    """Ratchet-run intent addendum (§6): make the finding class structurally
    impossible, citing the existing instances."""
    lines = []
    for f in findings[:20]:
        paths = ", ".join((f.affected_paths or [])[:5]) or "n/a"
        lines.append(f"- {f.title} (paths: {paths})")
    listed = "\n".join(lines) or "- (instances exist but carry no titles)"
    return (
        "## Ratchet objective\n"
        f"This is a RATCHET ticket: the finding class `{tag}/{subtype}` has recurred\n"
        f"{len(findings)} time(s) in this repository. Do NOT just fix instances — add a lint rule,\n"
        "CI check, or test that STRUCTURALLY prevents new instances of this class from being\n"
        "introduced (fail the build when one appears). Prefer extending existing lint/CI\n"
        "configuration over inventing new infrastructure.\n"
        "\n"
        f"Existing instances of `{tag}/{subtype}`:\n"
        f"{listed}\n"
    )


async def trigger_implement_run(
    ticket: Ticket,
    *,
    triggered_by: str = "",
    trigger: RunTrigger = RunTrigger.MANUAL,
    intent_addendum: str = "",
) -> Run:
    """Create the write sandbox and dispatch the implement run."""
    if (ticket.status or "") not in IMPLEMENTABLE_TICKET_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"ticket must have passed Gate 1 (status todo or in-progress) — it is "
                f"'{ticket.status}'"
            ),
        )
    repo = await require_repository(ticket.repository_uid, require_github=True)

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
    return await dispatch_serialized(
        target_uid=ticket.uid,
        playbook="implement",
        conflict_message="a write run is already in progress for this ticket",
        active_filter={"ticket_uid": ticket.uid},
        dispatch=_dispatch,
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
        },
        linked_ticket_uid=ticket.uid,
        executor=Executor.CLAUDE_CODE,
        execution_mode=ExecutionMode.IMPLEMENT,
        run_policy_uid=run_policy.uid,
        trigger=trigger,
        triggered_by=triggered_by,
        sandbox_factory=_make_sandbox,
    )


async def _docs_for_ticket(ticket: Ticket) -> list[str]:
    """Doc uids watching the paths this ticket's findings touch — for briefing
    pre-load. A ticket carries no paths itself, but its findings (origin +
    linked) do. Best-effort: any failure yields no pre-load (the briefing
    index + read_doc still cover every page)."""
    try:
        finding_uids = list(ticket.linked_finding_uids or [])
        if ticket.origin_finding_uid:
            finding_uids.append(ticket.origin_finding_uid)
        paths: list[str] = []
        for fu in dict.fromkeys(finding_uids):
            f = await Finding.nodes.get_or_none(uid=fu)
            if f:
                paths.extend(f.affected_paths or [])
        return await docs_watching_paths(ticket.repository_uid, paths)
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
    body = (
        f"OpenSweep-Ticket: {ticket.uid}\n"
        "\n"
        f"{(ticket.description or '').strip()}\n"
        "\n"
        "## Acceptance criteria\n"
        f"{ac_block}\n"
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
