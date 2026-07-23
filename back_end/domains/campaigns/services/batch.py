"""Batch campaigns — the audit-everything fan-out.

A batch parent owns NO parts of its own. It fans out into three child
campaigns (one subsystem, one feature, one global), each a normal campaign
that plans and dispatches independently, then rolls their digests up into
one parent summary once they all finish.

The parent's status mirrors the fleet: planning until launched, running
while any child is live, done once every child is terminal and aggregated.
campaign_service is imported lazily inside functions — it imports batch, so
a module-level import would be circular.
"""

from __future__ import annotations

from uuid import uuid4

from domains.campaigns.models import Campaign
from domains.campaigns.schemas import CreateCampaignRequest
from infrastructure.audit import write_audit

# The child kinds a batch fans out into, in dispatch order.
_CHILD_KINDS = ("subsystem", "feature", "global")

# Child statuses that end a child's contribution to the roll-up.
_TERMINAL = {"done", "failed", "cancelled"}


async def create_batch(
    repository_uid: str,
    req: CreateCampaignRequest,
    *,
    created_by: str = "",
    trigger_provenance: str = "manual",
) -> Campaign:
    """Create the batch parent (kind="batch", no parts) plus three child
    campaigns (subsystem/feature/global), sharing effort/selection/coverage,
    each with its default per-kind lenses and parent_uid set. Returns the
    parent with child_uids populated.

    campaign_service.create is called per child kind (lazy import) so the
    children plan through the exact same path a standalone campaign would."""
    from domains.campaigns.services import campaign_service

    parent = Campaign(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        title=req.title or "Audit-everything batch",
        status="planning",
        template=(req.template or "full").strip(),
        kind="batch",
        selection=(req.selection or "all").strip() or "all",
        coverage_keys=list(req.coverage_keys or []),
        effort=(req.effort or "").strip(),
        k=max(int(req.k or 3), 1),
        area_prefix=(req.area_prefix or "").strip(),
        parts=[],
        max_parallel=max(int(req.max_parallel or 2), 1),
        created_by=created_by,
        trigger_provenance=trigger_provenance or "manual",
    )
    await parent.save()

    child_uids: list[str] = []
    for child_kind in _CHILD_KINDS:
        child_req = CreateCampaignRequest(
            kind=child_kind,
            selection=(req.selection or "all").strip() or "all",
            coverage_keys=list(req.coverage_keys or []),
            effort=(req.effort or "").strip(),
            k=max(int(req.k or 3), 1),
            area_prefix=(req.area_prefix or "").strip(),
            max_parallel=max(int(req.max_parallel or 2), 1),
            title=f"{parent.title} — {child_kind}",
        )
        child = await campaign_service.create(
            repository_uid,
            child_req,
            created_by=created_by,
            trigger_provenance=trigger_provenance or "manual",
        )
        child.parent_uid = parent.uid
        await child.save()
        child_uids.append(child.uid)

    parent.child_uids = child_uids
    await parent.save()
    await campaign_service.record_event(
        parent, "batch_planned", children=len(child_uids)
    )
    await write_audit(
        kind="campaign.batch_planned",
        subject_uid=parent.uid,
        subject_type="Campaign",
        actor_uid=created_by,
        repository_uid=repository_uid,
        payload={"children": child_uids},
    )
    return parent


async def launch_batch(parent: Campaign) -> None:
    """Launch every child campaign, then move the parent to running.

    Each child launches through campaign_service.launch (its own replan +
    dispatch); the parent just tracks the fleet. A child that fails to launch
    is immediately cancelled (planning → cancelled, a legal transition) so
    aggregate_batch sees it as terminal and the parent can finalize rather
    than hanging in running forever."""
    from domains.campaigns.models import is_legal_status_transition
    from domains.campaigns.services import campaign_service

    launched = 0
    for uid in list(parent.child_uids or []):
        try:
            await campaign_service.launch(uid, actor_uid=parent.created_by or "")
            launched += 1
        except Exception as exc:  # noqa: BLE001 — one bad child never stalls the batch
            err_msg = f"{type(exc).__name__}: {exc}"
            await campaign_service.record_event(
                parent, "batch_child_launch_failed", child=uid, error=err_msg
            )
            # Transition the failed child to cancelled so aggregate_batch treats
            # it as terminal.  We use cancel() which performs the legal
            # planning → cancelled move; if that itself raises (e.g. the child
            # was already terminal) we swallow the error so the other children
            # still get launched.
            try:
                await campaign_service.cancel(
                    uid,
                    reason=f"batch child failed to launch: {err_msg}",
                    actor_uid=parent.created_by or "",
                )
            except Exception:  # noqa: BLE001 — best-effort; must not stall the loop
                pass

    fresh = await Campaign.nodes.get_or_none(uid=parent.uid) or parent
    if is_legal_status_transition(fresh.status or "planning", "running"):
        fresh.status = "running"
        await fresh.save()
    await campaign_service.record_event(fresh, "batch_launched", launched=launched)


async def aggregate_batch(parent: Campaign) -> bool:
    """Roll a batch parent's children up when they are ALL terminal.

    Returns False (no-op) while any child is still live. Once every child is
    terminal (done/failed/cancelled) it builds parent.summary =
    {children: [{uid, kind, status, counts}], totals: {...}} from each child's
    own summary.counts and transitions the parent → done; returns True."""
    from domains.campaigns.services import campaign_service

    children = []
    for uid in list(parent.child_uids or []):
        child = await Campaign.nodes.get_or_none(uid=uid)
        if child is not None:
            children.append(child)

    if not children or any(
        (c.status or "planning") not in _TERMINAL for c in children
    ):
        return False

    child_rows = []
    totals: dict[str, int] = {}
    for c in children:
        counts = dict((c.summary or {}).get("counts") or {})
        child_rows.append(
            {
                "uid": c.uid,
                "kind": str(getattr(c, "kind", "") or ""),
                "status": c.status or "",
                "counts": counts,
            }
        )
        totals["total"] = totals.get("total", 0) + int(counts.get("total") or 0)

    fresh = await Campaign.nodes.get_or_none(uid=parent.uid) or parent
    # A batch parent only aggregates from running, and moves running →
    # finalizing → done (the status matrix forbids running → done directly).
    # Any other state (already terminal, or a concurrent cancel) is a no-op.
    if (fresh.status or "") != "running":
        return False
    fresh.summary = {"children": child_rows, "totals": totals}
    fresh.status = "finalizing"
    await fresh.save()
    fresh.status = "done"
    await fresh.save()
    await campaign_service.record_event(
        fresh, "batch_aggregated", children=len(child_rows), total=totals.get("total", 0)
    )
    await write_audit(
        kind="campaign.batch_completed",
        subject_uid=fresh.uid,
        subject_type="Campaign",
        actor_uid=fresh.created_by or "campaign",
        repository_uid=fresh.repository_uid,
        payload={"totals": totals, "children": len(child_rows)},
    )
    return True
