"""FindingResolution lifecycle — the per-PR ledger (PLATFORM_V2_DESIGN.md §4).

State machine:

    open ──► in-fix ──► fixed(sha) ──► verified          (terminal, good)
      │                    │
      │                    └──► reopened (re-review still sees it)
      ├──► deferred (auto-creates linked Ticket)
      └──► waived   (reason required; suppresses re-discovery via the Finding)

"fixed is claimed, verified is granted": fixers (agent or human) may set
fixed(sha); only a review/verification run may set verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.delivery.models import FindingResolution, MergePolicy, PullRequest, resolution_key
from domains.delivery.schemas import (
    BlockingOverride,
    FindingResolutionDTO,
    MergePolicyDTO,
    ResolutionState,
)
from domains.delivery.services.convergence import resolution_is_blocking
from domains.findings.models import Finding
from domains.findings.schemas import FindingStatus
from domains.tickets.models import Ticket
from infrastructure.audit import write_audit

_FIXABLE_STATES = {"open", "in-fix", "reopened"}
_TRIAGEABLE_STATES = {"open", "in-fix", "fixed", "reopened"}

# Finding statuses a waive must NOT overwrite: these are already settled.
# Everything else (open, acknowledged, wont-fix itself) flips to WONT_FIX so
# the dedupe key suppresses re-discovery (§4: waive once, suppress forever).
TERMINAL_FINDING_STATUSES = {
    FindingStatus.ACCEPTED.value,
    FindingStatus.SUPERSEDED.value,
    FindingStatus.DISMISSED.value,
    FindingStatus.FIXED.value,
}


def finding_flips_to_wont_fix(status: str) -> bool:
    """Pure guard: may a waive flip this Finding to wont-fix?"""
    return (status or FindingStatus.OPEN.value) not in TERMINAL_FINDING_STATUSES


def initial_resolution_state_for_finding(status: str, evidence: dict | None) -> tuple[str, str]:
    """(state, waive_reason) a fresh resolution should be born with.

    A wont-fix Finding was already waived once — binding it to a new PR must
    not resurrect it as an open blocker (the suppression hole)."""
    if (status or "") == FindingStatus.WONT_FIX.value:
        reason = str((evidence or {}).get("waive_reason") or "") or "finding is wont-fix"
        return "waived", reason
    return "open", ""


async def ensure_merge_policy(repository_uid: str) -> MergePolicy:
    policy = await MergePolicy.nodes.get_or_none(repository_uid=repository_uid)
    if policy is None:
        policy = MergePolicy(uid=uuid4().hex, repository_uid=repository_uid)
        await policy.save()
    return policy


def merge_policy_to_dto(p: MergePolicy) -> MergePolicyDTO:
    from domains.delivery.models import DEFAULT_PATH_DENYLIST

    # None (pre-Phase-3 node) → defaults; explicit [] is an operator opt-out.
    denylist = p.path_denylist if p.path_denylist is not None else list(DEFAULT_PATH_DENYLIST)
    return MergePolicyDTO(
        uid=p.uid,
        repository_uid=p.repository_uid,
        blocking=dict(p.blocking or {}),
        require_clean_round=bool(p.require_clean_round),
        max_fix_rounds=int(p.max_fix_rounds or 0),
        path_denylist=[str(x) for x in denylist],
    )


def resolution_to_dto(
    r: FindingResolution, finding: Finding | None, blocking_policy: dict
) -> FindingResolutionDTO:
    severity = (finding.severity if finding else "medium") or "medium"
    tags = list(finding.tags or []) if finding else []
    return FindingResolutionDTO(
        uid=r.uid,
        finding_uid=r.finding_uid,
        pull_request_uid=r.pull_request_uid,
        repository_uid=r.repository_uid,
        introduced_at_sha=r.introduced_at_sha or "",
        state=ResolutionState(r.state or "open"),
        fixed_at_sha=r.fixed_at_sha or "",
        verified_at_sha=r.verified_at_sha or "",
        verified_by_run_uid=r.verified_by_run_uid or "",
        waived_by=r.waived_by or "",
        waive_reason=r.waive_reason or "",
        waive_requested_by=r.waive_requested_by or "",
        waive_requested_reason=r.waive_requested_reason or "",
        blocking_override=BlockingOverride(r.blocking_override or ""),
        blocking_override_reason=r.blocking_override_reason or "",
        ticket_uid=r.ticket_uid or "",
        blocking=resolution_is_blocking(
            state=r.state or "open",
            severity=severity,
            tags=tags,
            blocking_policy=blocking_policy,
            override=r.blocking_override or "",
        ),
        created_at=r.created_at,
        updated_at=r.updated_at,
        finding_title=(finding.title if finding else "") or "",
        finding_severity=severity,
        finding_tags=tags,
    )


class ResolutionService:
    async def get_node(self, uid: str) -> FindingResolution:
        r = await FindingResolution.nodes.get_or_none(uid=uid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"FindingResolution {uid} not found")
        return r

    async def ensure(
        self, *, finding_uid: str, pull_request_uid: str, introduced_at_sha: str = ""
    ) -> FindingResolution:
        """Idempotent bind of a Finding to a PR."""
        key = resolution_key(finding_uid, pull_request_uid)
        existing = await FindingResolution.nodes.get_or_none(resolution_key=key)
        if existing is not None:
            return existing
        finding = await Finding.nodes.get_or_none(uid=finding_uid)
        if finding is None:
            raise HTTPException(status_code=404, detail=f"Finding {finding_uid} not found")
        pr = await PullRequest.nodes.get_or_none(uid=pull_request_uid)
        if pr is None:
            raise HTTPException(status_code=404, detail=f"PullRequest {pull_request_uid} not found")
        # Tenancy choke point (F1): a Finding may only be bound to a PR in its
        # OWN repository. The platform-tool caller is pinned to the PR's repo,
        # but finding_uid is client-supplied — without this every caller of
        # ensure() (incl. the MCP bind-finding tool) could launder another
        # org's Finding into this PR's convergence ledger. 404, not 409, so a
        # foreign uid never leaks its existence.
        if finding.repository_uid != pr.repository_uid:
            raise HTTPException(status_code=404, detail=f"Finding {finding_uid} not found")
        state, waive_reason = initial_resolution_state_for_finding(
            finding.status or "", dict(finding.evidence or {})
        )
        r = FindingResolution(
            uid=uuid4().hex,
            finding_uid=finding_uid,
            pull_request_uid=pull_request_uid,
            repository_uid=pr.repository_uid,
            resolution_key=key,
            introduced_at_sha=introduced_at_sha or pr.head_sha or "",
            state=state,
            waive_reason=waive_reason,
            waived_by="system" if state == "waived" else "",
        )
        await r.save()
        await write_audit(
            kind="resolution.bound",
            subject_uid=r.uid,
            subject_type="FindingResolution",
            payload={
                "finding_uid": finding_uid,
                "pull_request_uid": pull_request_uid,
                "state": state,
            },
        )
        # Cross-link into the delivery loop's origin: a finding bound to a
        # ticket-owned PR also lands on the ticket (idempotent), so the
        # work item shows what its reviews found and future implement runs
        # inherit it as context. Best-effort — the PR ledger is authoritative.
        ticket_uid = str(getattr(pr, "ticket_uid", "") or "")
        if ticket_uid:
            try:
                from domains.tickets.services.ticket_service import TicketService

                await TicketService().link_finding(
                    ticket_uid, finding_uid, actor_uid="system"
                )
            except Exception as exc:  # noqa: BLE001
                from logging_config import logger

                logger.warning(
                    f"ticket link for finding {finding_uid} → ticket {ticket_uid} "
                    f"failed: {type(exc).__name__}: {exc}",
                    extra={"tag": "delivery"},
                )
        return r

    async def list_for_pr(self, pull_request_uid: str) -> list[FindingResolutionDTO]:
        pr = await PullRequest.nodes.get_or_none(uid=pull_request_uid)
        if pr is None:
            raise HTTPException(status_code=404, detail=f"PullRequest {pull_request_uid} not found")
        policy = await ensure_merge_policy(pr.repository_uid)
        nodes = await FindingResolution.nodes.filter(pull_request_uid=pull_request_uid)
        out = []
        for r in nodes:
            finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
            out.append(resolution_to_dto(r, finding, dict(policy.blocking or {})))
        out.sort(key=lambda d: (not d.blocking, d.state.value, d.finding_severity))
        return out

    async def _save(self, r: FindingResolution, *, audit_kind: str, actor_uid: str | None, payload: dict):
        r.updated_at = datetime.now(UTC)
        await r.save()
        await write_audit(
            kind=audit_kind,
            subject_uid=r.uid,
            subject_type="FindingResolution",
            actor_uid=actor_uid,
            payload=payload,
        )

    async def attach_fix(self, uid: str, *, sha: str, actor_uid: str | None = None) -> FindingResolution:
        r = await self.get_node(uid)
        if r.state not in _FIXABLE_STATES:
            raise HTTPException(status_code=409, detail=f"cannot attach fix in state {r.state}")
        r.state = "fixed"
        r.fixed_at_sha = sha
        await self._save(r, audit_kind="resolution.fixed", actor_uid=actor_uid, payload={"sha": sha})
        return r

    async def verify(
        self, uid: str, *, sha: str, run_uid: str = "", actor_uid: str | None = None
    ) -> FindingResolution:
        r = await self.get_node(uid)
        if r.state != "fixed":
            raise HTTPException(status_code=409, detail=f"only fixed resolutions can be verified (state={r.state})")
        r.state = "verified"
        r.verified_at_sha = sha
        r.verified_by_run_uid = run_uid or ""
        await self._save(
            r, audit_kind="resolution.verified", actor_uid=actor_uid, payload={"sha": sha, "run_uid": run_uid}
        )
        return r

    async def reopen(self, uid: str, *, actor_uid: str | None = None, reason: str = "") -> FindingResolution:
        r = await self.get_node(uid)
        r.state = "reopened"
        await self._save(r, audit_kind="resolution.reopened", actor_uid=actor_uid, payload={"reason": reason})
        return r

    async def refute(
        self, uid: str, *, run_uid: str, reasoning: str = ""
    ) -> FindingResolution:
        """Verification run disproved the finding at the reviewed sha —
        machine verdict, distinct from a human waive (which suppresses
        re-discovery forever). Refuted never blocks."""
        r = await self.get_node(uid)
        if r.state not in _TRIAGEABLE_STATES:
            raise HTTPException(status_code=409, detail=f"cannot refute in state {r.state}")
        r.state = "refuted"
        await self._save(
            r,
            audit_kind="resolution.refuted",
            actor_uid=run_uid,
            payload={"run_uid": run_uid, "reasoning": reasoning},
        )
        return r

    async def waive(self, uid: str, *, reason: str, actor_uid: str | None = None) -> FindingResolution:
        """Waive on this PR AND mark the Finding wont-fix so the dedupe key
        suppresses re-discovery (§4: waive once, suppress forever)."""
        r = await self.get_node(uid)
        if r.state not in _TRIAGEABLE_STATES:
            raise HTTPException(status_code=409, detail=f"cannot waive in state {r.state}")
        r.state = "waived"
        r.waived_by = actor_uid or "(unknown)"
        r.waive_reason = reason
        await self._save(r, audit_kind="resolution.waived", actor_uid=actor_uid, payload={"reason": reason})

        # Flip the Finding from ANY non-terminal status (not just OPEN) —
        # e.g. an acknowledged finding waived on a PR must still be
        # suppressed from re-discovery. Terminal statuses stay untouched.
        finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
        if finding is not None and finding_flips_to_wont_fix(finding.status or ""):
            finding.status = FindingStatus.WONT_FIX.value
            finding.evidence = {**(finding.evidence or {}), "waive_reason": reason}
            finding.updated_at = datetime.now(UTC)
            await finding.save()
        return r

    async def defer(self, uid: str, *, actor_uid: str | None = None) -> tuple[FindingResolution, Ticket]:
        """Later → ticket: one click, evidence preserved (§4)."""
        r = await self.get_node(uid)
        if r.state not in _TRIAGEABLE_STATES:
            raise HTTPException(status_code=409, detail=f"cannot defer in state {r.state}")
        finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
        title = (finding.title if finding else None) or f"Deferred finding {r.finding_uid[:8]}"
        description_parts = []
        if finding is not None:
            if finding.why_it_matters:
                description_parts.append(f"**Why it matters**\n{finding.why_it_matters}")
            if finding.suggested_fix:
                description_parts.append(f"**Suggested fix**\n{finding.suggested_fix}")
            if finding.affected_paths:
                paths = "\n".join(f"- {p}" for p in finding.affected_paths)
                description_parts.append(f"**Affected paths**\n{paths}")
        description_parts.append(f"Deferred from PR resolution `{r.uid}` (finding `{r.finding_uid}`).")

        ticket = Ticket(
            uid=uuid4().hex,
            repository_uid=r.repository_uid,
            title=title,
            description="\n\n".join(description_parts),
            labels=list(finding.tags or []) if finding else [],
            origin="finding",
            origin_finding_uid=r.finding_uid,
            linked_finding_uids=[r.finding_uid],
        )
        await ticket.save()

        r.state = "deferred"
        r.ticket_uid = ticket.uid
        await self._save(
            r, audit_kind="resolution.deferred", actor_uid=actor_uid, payload={"ticket_uid": ticket.uid}
        )
        return r, ticket

    async def request_waiver(self, uid: str, *, reason: str, actor_uid: str | None = None) -> FindingResolution:
        """Agent-side waiver request — no state change, surfaces in Needs-You (§11)."""
        r = await self.get_node(uid)
        if r.state not in _TRIAGEABLE_STATES:
            raise HTTPException(status_code=409, detail=f"cannot request waiver in state {r.state}")
        r.waive_requested_by = actor_uid or "(unknown)"
        r.waive_requested_reason = reason
        await self._save(
            r, audit_kind="resolution.waive_requested", actor_uid=actor_uid, payload={"reason": reason}
        )
        return r

    async def set_blocking_override(
        self, uid: str, *, override: str, reason: str, actor_uid: str | None = None
    ) -> FindingResolution:
        r = await self.get_node(uid)
        r.blocking_override = override
        r.blocking_override_reason = reason
        await self._save(
            r,
            audit_kind="resolution.blocking_override",
            actor_uid=actor_uid,
            payload={"override": override, "reason": reason},
        )
        return r
