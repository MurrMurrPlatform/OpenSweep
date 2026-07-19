"""In-flight run discovery + dispatch guards.

Overlap rules (read+write): read-only runs (review, verify, ask) may overlap
each other and one write run on the same target; two WRITE runs
(implement/fix) would fight over the same branch and ledger, and two runs of
the SAME playbook would double their ledger writes (double findings, double
verdicts) — those two combinations 409. Chat runs are conversations, not
work: they never block a dispatch and are never blocked. `blocking_run`
answers "does this dispatch conflict?"; `conflict_detail` builds the
structured 409 payload the UI needs to LINK to the in-flight run instead of
showing an opaque error string.

V3: runs carry their entity links directly (linked_pr_uid/linked_ticket_uid),
so the guard no longer joins through Investigations.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from domains.runs.models import Run

# Statuses that mean "an executor is (or will be) working on this target".
# paused_quota counts: the resume beat re-dispatches the SAME run.
# awaiting_input does NOT count — the turn is over; a follow-up is a new turn.
ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "paused_quota"})

# Playbooks that commit into a work branch — two of these on one target race
# over the same branch and fix-round ledger.
WRITE_PLAYBOOKS = frozenset({"implement", "fix", "thread"})


def blocking_run(active: Iterable[Any], *, playbook: str) -> Any | None:
    """First active run that conflicts with dispatching `playbook` on the
    same target, or None. Pure — unit-testable without Neo4j.

    Conflicts: same playbook (duplicate ledger writes), or write-vs-write
    (branch race). Chat runs never conflict in either direction.
    """
    if playbook == "chat":
        return None
    for run in active:
        other = getattr(run, "playbook", "") or ""
        if other == "chat":
            continue
        if other == playbook:
            return run
        if playbook in WRITE_PLAYBOOKS and other in WRITE_PLAYBOOKS:
            return run
    return None


def filter_active_runs(
    runs: Iterable[Any],
    *,
    repository_uid: str | None = None,
    pull_request_uid: str | None = None,
    ticket_uid: str | None = None,
    finding_uid: str | None = None,
    playbooks: Iterable[str] | None = None,
) -> list[Any]:
    """Pure filter over runs — unit-testable without Neo4j."""
    wanted_playbooks = {p for p in (playbooks or []) if p}
    out: list[Any] = []
    for run in runs:
        if (getattr(run, "status", "") or "") not in ACTIVE_RUN_STATUSES:
            continue
        if repository_uid and getattr(run, "repository_uid", "") != repository_uid:
            continue
        if pull_request_uid and (getattr(run, "linked_pr_uid", "") or "") != pull_request_uid:
            continue
        if ticket_uid and (getattr(run, "linked_ticket_uid", "") or "") != ticket_uid:
            continue
        if finding_uid and (getattr(run, "linked_finding_uid", "") or "") != finding_uid:
            continue
        if wanted_playbooks and (getattr(run, "playbook", "") or "") not in wanted_playbooks:
            continue
        out.append(run)
    return out


async def active_runs_for(
    repository_uid: str | None = None,
    pull_request_uid: str | None = None,
    ticket_uid: str | None = None,
    finding_uid: str | None = None,
    playbooks: list[str] | None = None,
) -> list[Run]:
    """Active (queued/running/paused_quota) runs matching the given links
    and/or playbooks. Python-side filtering is fine at the current scale.

    Repairs stuck rows first: a run orphaned by a process restart must not
    hold the 409 dispatch guard hostage until someone happens to open the
    runs list."""
    from domains.runs.services.run_reconciliation import reconcile_stale_runs

    await reconcile_stale_runs()
    runs = await Run.nodes.filter(status__in=list(ACTIVE_RUN_STATUSES))
    return filter_active_runs(
        runs,
        repository_uid=repository_uid,
        pull_request_uid=pull_request_uid,
        ticket_uid=ticket_uid,
        finding_uid=finding_uid,
        playbooks=playbooks,
    )


def conflict_detail(message: str, run: Any) -> dict[str, str]:
    """The 409 payload shape the UI relies on to deep-link the active run."""
    return {
        "message": message,
        "run_uid": getattr(run, "uid", "") or "",
        "scheduled_agent_uid": getattr(run, "scheduled_agent_uid", "") or "",
    }
