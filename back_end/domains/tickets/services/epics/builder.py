"""Build epics from a rule, without an agent.

This is the producer that answers "take the top 12 criticals and work them in
4 runs" directly: load facts, select, partition, persist as proposals. It runs
synchronously — there is no run to dispatch, because none of the six
deterministic axes requires judgment. Only `root-cause` does, and that stays
with the grouping agent.

Every producer converges on `EpicService.propose`, so a rule-built
epic and an agent-built one are the same object and go through the same
single review gate.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from domains.tickets.schemas import EpicProposalDTO, PlanEpicsRequest
from domains.tickets.services.epic_service import (
    EpicService,
    proposal_to_dto,
)
from domains.tickets.services.epics.axes import partition
from domains.tickets.services.epics.loader import load_ticket_facts
from domains.tickets.services.epics.schemas import (
    DETERMINISTIC_AXES,
    EpicAxis,
    EpicPartition,
    EpicSelection,
)
from domains.tickets.services.epics.selection import select_tickets


def _axis_or_422(raw: str) -> EpicAxis:
    try:
        axis = EpicAxis(raw)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"unknown epic axis '{raw}'"
        ) from None
    if axis not in DETERMINISTIC_AXES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"axis '{raw}' is not computable — it requires agent judgment. "
                "Propose it instead via POST /api/v1/platform-tools/tickets/"
                "propose-group (tool opensweep_platform_propose_epic)."
            ),
        )
    return axis


async def plan_epics(
    req: PlanEpicsRequest, *, actor_uid: str
) -> dict[str, object]:
    """Select, partition, and (unless `dry_run`) persist a plan of epics.

    Returns the drafts either way so the caller can preview the exact plan it
    would get — the counts a reviewer approves against ("12 tickets → 4
    epics") must come from the same code that builds them, not an estimate.
    """
    axis = _axis_or_422(req.axis)
    if req.max_members < req.min_members:
        raise HTTPException(
            status_code=422, detail="max_members must be >= min_members"
        )

    facts = await load_ticket_facts(req.repository_uid)
    selected = select_tickets(
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
    drafts = partition(
        selected,
        EpicPartition(
            axis=axis,
            max_epics=req.max_epics,
            min_members=req.min_members,
            max_members=req.max_members,
        ),
    )

    summary = {
        "axis": axis.value,
        "candidates": len(facts),
        "selected": len(selected),
        "epics": len(drafts),
        # Selected-but-unplaced tickets are not a failure — most axes leave
        # singletons behind by design. Reporting the number stops the plan
        # from reading as "these 12 were handled" when 5 were dropped.
        "unplaced": len(selected) - sum(len(d.member_ticket_uids) for d in drafts),
    }

    if req.dry_run:
        return {
            "plan_uid": "",
            "summary": summary,
            "epics": [
                {
                    "title": d.title,
                    "axis": d.axis.value,
                    "evidence": d.evidence,
                    "rationale": d.rationale,
                    "member_ticket_uids": d.member_ticket_uids,
                    "suggested_priority": d.suggested_priority,
                    "suggested_labels": d.suggested_labels,
                }
                for d in drafts
            ],
        }

    plan_uid = uuid4().hex
    service = EpicService()
    created: list[EpicProposalDTO] = []
    errors: list[dict[str, str]] = []
    for d in drafts:
        # `propose` re-reads each member ticket through `validate_group_members`
        # and raises HTTPException on staleness (member deleted, moved to
        # `done`, wrong repo). Without the try, the FIRST stale draft aborts
        # the whole loop and returns nothing — but every draft persisted
        # before it is already saved with `plan_uid`, so those proposals are
        # orphaned in the DB with no way for the caller to discover them.
        # Mirror bulk_approve's contract: skip the failing draft, keep going,
        # and always return the plan_uid + per-draft errors.
        try:
            proposal, _deduped = await service.propose(
                repository_uid=req.repository_uid,
                title=d.title,
                rationale=d.rationale,
                member_ticket_uids=d.member_ticket_uids,
                suggested_labels=d.suggested_labels,
                suggested_priority=d.suggested_priority,
                actor_uid=actor_uid,
                axis=d.axis.value,
                evidence=d.evidence,
                plan_uid=plan_uid,
                origin="rule",
            )
        except HTTPException as exc:
            errors.append({"title": d.title, "detail": str(exc.detail)})
            continue
        except Exception as exc:  # noqa: BLE001 — same reason as bulk_approve
            import logging

            logging.getLogger(__name__).warning(
                "plan_epics: unexpected error for draft %r: %s",
                d.title,
                exc,
                exc_info=True,
            )
            errors.append(
                {"title": d.title, "detail": f"internal error: {type(exc).__name__}"}
            )
            continue
        created.append(proposal_to_dto(proposal))

    summary = {**summary, "errors": len(errors)}
    return {
        "plan_uid": plan_uid,
        "summary": summary,
        "epics": created,
        "errors": errors,
    }
