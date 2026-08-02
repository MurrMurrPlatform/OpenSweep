"""Campaign finalize: fold the parts' outcomes into one digest.

`build_summary` is pure (counts + coverage + holes); `finalize_campaign`
loads each part's findings (source_run_uid / source_run_uids match) and
Checked coverage stamps, saves the summary, and closes the campaign —
done, or failed when EVERY part failed. Idempotent: the tick re-runs it
for rows stranded in finalizing by a crash.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from infrastructure.audit import write_audit
from logging_config import logger


def _feature_rollup(parts: list[dict]) -> list[dict]:
    """Aggregate feature-leaf parts up to their parent feature grouping.

    Feature parts are always LEAVES (the planner never emits a part for a
    parent feature grouping). This rolls each leaf's coverage/state up to
    its parent key (the key minus its last "/" segment; a top-level feature
    leaf rolls up under itself) so the digest can render the parent →
    sub-feature tree with aggregated coverage. Pure.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for p in parts:
        if (p.get("kind") or "area") != "feature":
            continue
        keys = [str(k) for k in (p.get("area_keys") or []) if k]
        leaf_key = keys[0] if keys else ""
        parent = leaf_key.rsplit("/", 1)[0] if "/" in leaf_key else leaf_key
        g = groups.get(parent)
        if g is None:
            g = {
                "feature_key": parent,
                "covered": 0,
                "skipped": 0,
                "findings": 0,
                "leaves": [],
                "done": 0,
            }
            groups[parent] = g
            order.append(parent)
        g["covered"] += int(p.get("covered") or 0)
        g["skipped"] += int(p.get("skipped") or 0)
        g["leaves"].append(
            {
                "area_key": leaf_key,
                "idx": int(p["idx"]),
                "title": p.get("title") or "",
                "covered": int(p.get("covered") or 0),
                "skipped": int(p.get("skipped") or 0),
                "state": p.get("state") or "pending",
            }
        )
        if (p.get("state") or "") == "done":
            g["done"] += 1
    out: list[dict] = []
    for parent in order:
        g = groups[parent]
        total = len(g["leaves"])
        g["leaf_count"] = total
        # Parent coverage state: covered when every sub-feature leaf ran,
        # partial when some did, none when none did.
        g["state"] = (
            "covered" if g["done"] == total else "partial" if g["done"] else "uncovered"
        )
        g.pop("done", None)
        out.append(g)
    return out


def _lens_rollup(parts: list[dict]) -> list[dict]:
    """Per-lens verdict tallies across the campaign's parts. Pure.

    `lens_verdicts` is what the coverage contract exists to collect — the
    agent saying, per discipline, "checked and clean" / "checked, filed
    something" / "skipped". Every area run was asked for it, complete_run
    validated it, and Checked stored it; then the digest discarded it and
    reported finding COUNTS instead. "performance skipped in 4 of 11 parts"
    is the honest coverage statement that was already being gathered.

    A lens with no returned verdict counts as `missing`, deliberately NOT
    folded into `skipped`: an agent saying "I skipped performance" and an
    agent silently omitting it are different failures, and only the first is
    the contract working.
    """
    tally: dict[str, dict] = {}
    order: list[str] = []
    for part in parts:
        verdicts = {
            str(v.get("lens") or ""): str(v.get("verdict") or "")
            for v in (part.get("lens_verdicts") or [])
            if isinstance(v, dict)
        }
        for lens in (str(k) for k in (part.get("lens_keys") or []) if k):
            row = tally.get(lens)
            if row is None:
                row = {
                    "lens": lens,
                    "checked-clean": 0,
                    "checked-findings": 0,
                    "skipped": 0,
                    "missing": 0,
                    "parts": 0,
                }
                tally[lens] = row
                order.append(lens)
            row["parts"] += 1
            verdict = verdicts.get(lens) or "missing"
            row[verdict if verdict in row else "missing"] += 1
    return [tally[lens] for lens in order]


def build_summary(
    parts: list[dict],
    findings_by_part: dict[int, list[dict]],
    holes: list[str],
) -> dict:
    """The campaign digest. Pure.

    `findings_by_part` maps part idx → [{severity, tags, title}]; parts may
    carry `covered`/`skipped` counts (from their Checked stamps). `holes`
    are the scope paths of failed/never-run parts — the coverage debt this
    campaign leaves behind. `coverage.feature_rollup` aggregates feature-leaf
    parts up to their parent feature grouping (parent-feature health).
    """
    by_severity: Counter[str] = Counter()
    by_part: dict[int, int] = {}
    for idx, findings in findings_by_part.items():
        by_part[int(idx)] = len(findings)
        for f in findings:
            by_severity[str(f.get("severity") or "medium")] += 1
    # Fold each feature part's finding count into its rollup group.
    rollup = _feature_rollup(parts)
    by_leaf_idx = {int(p["idx"]): p for p in parts}
    for g in rollup:
        g["findings"] = sum(
            by_part.get(leaf["idx"], 0) for leaf in g["leaves"] if leaf["idx"] in by_leaf_idx
        )
    return {
        "counts": {
            "by_severity": dict(by_severity),
            "by_part": {str(k): v for k, v in sorted(by_part.items())},
            "total": sum(by_part.values()),
        },
        "coverage": {
            "parts": [
                {
                    "idx": int(p["idx"]),
                    "title": p.get("title") or "",
                    "covered": int(p.get("covered") or 0),
                    "skipped": int(p.get("skipped") or 0),
                    "observed": int(p.get("observed") or 0),
                    "coverage_source": str(p.get("coverage_source") or "unknown"),
                    "state": p.get("state") or "pending",
                }
                for p in sorted(parts, key=lambda p: int(p["idx"]))
            ],
            "holes": list(holes),
            "feature_rollup": rollup,
            "lens_rollup": _lens_rollup(parts),
        },
        "failed_parts": sorted(
            int(p["idx"]) for p in parts if p.get("state") == "failed"
        ),
    }


async def compute_digest(campaign) -> tuple[dict, list]:
    """(summary, the campaign's Finding rows) from its parts' runs.

    Split out of finalize_campaign so the SYNTHESIS part can be handed the
    same numbers mid-flight: it dispatches while the campaign is still
    running, so `campaign.summary` is not written yet, and a synthesis run
    working from an empty digest would report a campaign that found nothing.
    """
    from domains.checked.models import Checked
    from domains.findings.models import Finding

    parts = [dict(p) for p in (campaign.parts or [])]
    run_by_idx = {int(p["idx"]): p.get("run_uid") or "" for p in parts}

    findings = list(await Finding.nodes.filter(repository_uid=campaign.repository_uid))
    findings_by_part: dict[int, list[dict]] = {}
    # The Finding objects themselves, deduped — the digest only needs a
    # projection, but the verification pass needs the rows.
    campaign_findings: dict[str, object] = {}
    for idx, run_uid in run_by_idx.items():
        if not run_uid:
            continue
        matched = [
            f
            for f in findings
            if (f.source_run_uid or "") == run_uid or run_uid in (f.source_run_uids or [])
        ]
        if matched:
            findings_by_part[idx] = [
                {"severity": f.severity or "medium", "tags": list(f.tags or []), "title": f.title}
                for f in matched
            ]
            for f in matched:
                campaign_findings[f.uid] = f

    # Coverage from the runs' Checked stamps (complete_run contract). The
    # lens verdicts ride along: they are what the contract was collecting all
    # along, and the digest used to drop them on the floor.
    coverage_by_run: dict[str, dict] = {}
    try:
        for c in await Checked.nodes.filter(repository_uid=campaign.repository_uid):
            if c.run_uid in run_by_idx.values():
                coverage_by_run[c.run_uid] = {
                    "covered": len(c.covered_paths or []),
                    "skipped": len(c.skipped_paths or []),
                    "observed": len(c.covered_paths_observed or []),
                    "coverage_source": c.coverage_source or "unknown",
                    "lens_verdicts": list(c.lens_verdicts or []),
                }
    except Exception as exc:  # noqa: BLE001 — coverage counts are best-effort
        logger.warning(
            f"campaign {campaign.uid}: coverage stamps unavailable: {exc}",
            extra={"tag": "campaigns"},
        )
    for p in parts:
        stamp = coverage_by_run.get(p.get("run_uid") or "") or {}
        p["covered"] = int(stamp.get("covered") or 0)
        p["skipped"] = int(stamp.get("skipped") or 0)
        p["observed"] = int(stamp.get("observed") or 0)
        p["coverage_source"] = stamp.get("coverage_source") or "unknown"
        p["lens_verdicts"] = list(stamp.get("lens_verdicts") or [])

    holes = [
        path
        for p in parts
        if p.get("state") != "done"
        for path in (p.get("scope_paths") or [])
    ]
    summary = build_summary(parts, findings_by_part, holes)
    return summary, list(campaign_findings.values())


async def finalize_campaign(campaign) -> None:
    """finalizing → done|failed, with the digest saved and audited."""
    from domains.campaigns.models import Campaign, is_legal_status_transition

    parts = [dict(p) for p in (campaign.parts or [])]
    summary, campaign_findings = await compute_digest(campaign)

    all_failed = bool(parts) and all(p.get("state") == "failed" for p in parts)
    to_status = "failed" if all_failed else "done"

    fresh = await Campaign.nodes.get_or_none(uid=campaign.uid) or campaign
    if not is_legal_status_transition(fresh.status or "", to_status):
        return  # already finalized by a concurrent tick

    # Audit BEFORE the status save: the notification feed derives from the
    # audit stream, and a crash between save and audit would be unrecoverable
    # (done→done is illegal, so re-entry returns above without ever
    # auditing). The reverse crash merely re-audits once — the lesser evil.
    await write_audit(
        kind="campaign.failed" if all_failed else "campaign.completed",
        subject_uid=fresh.uid,
        subject_type="Campaign",
        repository_uid=fresh.repository_uid,
        actor_uid=fresh.created_by or "campaign",
        payload={
            "title": fresh.title or "",
            "counts": summary["counts"],
            "failed_parts": summary["failed_parts"],
        },
    )

    now = datetime.now(UTC)
    fresh.summary = summary
    fresh.status = to_status
    fresh.events = [
        *(fresh.events or []),
        {"ts": now.isoformat(), "type": "finalized", "status": to_status},
    ]
    fresh.updated_at = now
    await fresh.save()

    # AFTER the status save, deliberately: everything above this line
    # re-executes when a crashed finalize is retried from `finalizing`
    # (tick.py's recovery loop), and dispatching a duplicate verification run
    # per crash would be the obvious way to get that wrong. Past this point
    # the row is terminal, so finalize_campaign returns at the legality gate
    # on any re-entry.
    await _verify_findings(fresh, campaign_findings)


async def _verify_findings(campaign, findings: list) -> None:
    """Dispatch the skeptic pass over this campaign's findings. Best-effort.

    Never raises: a campaign that has already reported its results must not be
    dragged back out of `done` because a follow-up dispatch failed. The
    single-shot marker means a failure here is not retried — deliberate, since
    the alternative is a campaign that re-dispatches a verify run on every
    beat of the tick.
    """
    from domains.campaigns.models import Campaign

    if not findings or (campaign.verification_run_uid or ""):
        return
    try:
        from domains.delivery.services.verification_run_service import (
            trigger_campaign_verification_run,
        )

        run = await trigger_campaign_verification_run(campaign, findings)
        if run is None:
            return
        fresh = await Campaign.nodes.get_or_none(uid=campaign.uid) or campaign
        fresh.verification_run_uid = run.uid
        await fresh.save()
        logger.info(
            f"campaign {campaign.uid}: verification run {run.uid} dispatched",
            extra={"tag": "campaigns"},
        )
    except Exception as exc:  # noqa: BLE001 — the campaign is already done
        logger.warning(
            f"campaign {campaign.uid}: verification dispatch failed: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "campaigns"},
        )
