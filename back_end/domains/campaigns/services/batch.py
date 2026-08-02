"""Batch campaigns — the audit-everything fan-out.

A batch parent owns NO parts of its own. It fans out into three child
campaigns (one subsystem, one feature, one global), each a normal campaign
that plans and dispatches independently, then rolls their digests up into
one parent summary once they all finish.

The parent's status mirrors the fleet: planning until launched, running
while any child is live, done once every child is terminal and aggregated.
campaign_service is imported lazily inside functions — it imports batch, so
a module-level import would be circular.

**The global child launches LAST, and that is load-bearing.** Inside one
campaign `tick.plan_tick` holds global parts until every area part is
terminal, so a global sweep's `escalate:<lens>` digest sees the whole
campaign's findings. A batch splits the kinds across three SEPARATE
campaigns, and the global child contains only global parts — its
`areas_terminal` check is `all([])`, vacuously true. Launching all three at
once therefore dispatched the whole-repo sweeps with an empty digest on a
first batch, and a one-cycle-stale digest afterwards: the batch defeated the
very ordering invariant the tick tests protect. `launch_batch` now holds the
global child in `planning` and `advance_batch` releases it once its siblings
are terminal.
"""

from __future__ import annotations

from uuid import uuid4

from domains.campaigns.models import DEFAULT_MAX_PARALLEL, Campaign
from domains.campaigns.schemas import CreateCampaignRequest
from infrastructure.audit import write_audit
from logging_config import logger

# The child kinds a batch fans out into, in dispatch order.
_CHILD_KINDS = ("subsystem", "feature", "global")

# Child kinds held back until their siblings finish (see the module docstring).
_DEFERRED_KINDS = {"global"}

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
        max_parallel=max(int(req.max_parallel or DEFAULT_MAX_PARALLEL), 1),
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
            max_parallel=max(int(req.max_parallel or DEFAULT_MAX_PARALLEL), 1),
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


async def _children(parent: Campaign) -> list[Campaign]:
    """The parent's child rows, skipping any that no longer exist."""
    out: list[Campaign] = []
    for uid in list(parent.child_uids or []):
        child = await Campaign.nodes.get_or_none(uid=uid)
        if child is not None:
            out.append(child)
    return out


async def _launch_child(parent: Campaign, uid: str) -> bool:
    """Launch one child; cancel it on failure. Returns whether it launched.

    A child that fails to launch is immediately cancelled (planning →
    cancelled, a legal transition) so aggregate_batch sees it as terminal and
    the parent can finalize rather than hanging in running forever."""
    from domains.campaigns.services import campaign_service

    try:
        await campaign_service.launch(uid, actor_uid=parent.created_by or "")
        return True
    except Exception as exc:  # noqa: BLE001 — one bad child never stalls the batch
        err_msg = f"{type(exc).__name__}: {exc}"
        await campaign_service.record_event(
            parent, "batch_child_launch_failed", child=uid, error=err_msg
        )
        try:
            await campaign_service.cancel(
                uid,
                reason=f"batch child failed to launch: {err_msg}",
                actor_uid=parent.created_by or "",
            )
        except Exception:  # noqa: BLE001 — best-effort; must not stall the loop
            pass
        return False


async def launch_batch(parent: Campaign) -> None:
    """Launch the batch's immediate children, then move the parent to running.

    Children whose kind is in `_DEFERRED_KINDS` are deliberately left in
    `planning` — `advance_batch` releases them once their siblings are
    terminal, so the global sweeps see a complete escalation queue (see the
    module docstring). Each other child launches through
    campaign_service.launch (its own replan + dispatch); the parent just
    tracks the fleet."""
    from domains.campaigns.models import is_legal_status_transition
    from domains.campaigns.services import campaign_service

    launched = 0
    deferred = 0
    for child in await _children(parent):
        if str(getattr(child, "kind", "") or "") in _DEFERRED_KINDS:
            deferred += 1
            continue
        if await _launch_child(parent, child.uid):
            launched += 1

    fresh = await Campaign.nodes.get_or_none(uid=parent.uid) or parent
    if is_legal_status_transition(fresh.status or "planning", "running"):
        fresh.status = "running"
        await fresh.save()
    await campaign_service.record_event(
        fresh, "batch_launched", launched=launched, deferred=deferred
    )


async def advance_batch(parent: Campaign) -> int:
    """Release deferred children whose siblings have all finished.

    Returns how many were launched this tick (0 = nothing to do, which is the
    common case). Called from the campaign tick before aggregate_batch: a
    child still sitting in `planning` is not terminal, so the roll-up cannot
    complete until this has run.

    A deferred child that fails to launch is cancelled by `_launch_child`,
    which keeps it terminal and lets the parent finalize instead of hanging.
    """
    from domains.campaigns.services import campaign_service

    children = await _children(parent)
    waiting = [
        c
        for c in children
        if str(getattr(c, "kind", "") or "") in _DEFERRED_KINDS
        and (c.status or "planning") == "planning"
    ]
    if not waiting:
        return 0
    siblings = [c for c in children if c.uid not in {w.uid for w in waiting}]
    if any((c.status or "planning") not in _TERMINAL for c in siblings):
        return 0

    launched = 0
    for child in waiting:
        logger.info(
            f"batch {parent.uid}: siblings terminal — launching deferred "
            f"{getattr(child, 'kind', '')!r} child {child.uid}",
            extra={"tag": "campaigns"},
        )
        if await _launch_child(parent, child.uid):
            launched += 1
    fresh = await Campaign.nodes.get_or_none(uid=parent.uid) or parent
    await campaign_service.record_event(
        fresh, "batch_deferred_launched", launched=launched
    )
    return launched


async def aggregate_batch(parent: Campaign) -> bool:
    """Roll a batch parent's children up when they are ALL terminal.

    Returns False (no-op) while any child is still live. Once every child is
    terminal (done/failed/cancelled) it builds parent.summary =
    {children: [{uid, kind, status, counts}], totals: {...}} from each child's
    own summary.counts and transitions the parent → done; returns True.

    A deferred child still in `planning` is not terminal, so this is also
    what keeps the parent open until `advance_batch` has released it."""
    from domains.campaigns.services import campaign_service

    children = await _children(parent)

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
