"""Platform tool: create_finding."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from domains.findings.models import Finding
from domains.findings.schemas import (
    FindingKind,
    FindingSize,
    FindingStatus,
    ParseStatus,
    Severity,
    SourcePath,
    normalize_tags,
)
from domains.findings.services.dedupe import (
    build_dedupe_key,
    path_basename,
    titles_similar,
)
from infrastructure.audit import write_audit
from logging_config import logger


def _enum_member(enum_cls, value: str, field: str):
    try:
        return enum_cls(value)
    except ValueError:
        expected = [member.value for member in enum_cls]
        raise HTTPException(
            status_code=422,
            detail=f"invalid {field}={value!r}; expected one of {expected}",
        ) from None


async def _merge_into(
    existing: Finding,
    *,
    confidence: float,
    evidence: Optional[dict[str, Any]],
    clean_tags: list[str],
    description: str,
    root_cause: str,
    why_it_matters: str,
    suggested_fix: str,
    detected_by_tool: str,
    detected_by_rule: str,
    source_run_uid: Optional[str],
) -> None:
    """Fold a re-filed duplicate into `existing` and record the confirmation."""
    existing.confidence = max(float(existing.confidence or 0.0), float(confidence))
    existing.evidence = {**(existing.evidence or {}), **(evidence or {})}
    merged_tags = normalize_tags([*(existing.tags or []), *clean_tags])
    existing.tags = merged_tags
    # Narrative fields only fill gaps on merge — never overwrite a
    # richer earlier analysis with a re-filed duplicate's text.
    if description and not (existing.description or "").strip():
        existing.description = description
    if root_cause and not (existing.root_cause or "").strip():
        existing.root_cause = root_cause
    if why_it_matters and not (existing.why_it_matters or "").strip():
        existing.why_it_matters = why_it_matters
    if suggested_fix and not (existing.suggested_fix or "").strip():
        existing.suggested_fix = suggested_fix
    # Attribute the earliest tool that surfaced it — only fill a gap, never
    # relabel a finding already credited to an analyzer.
    if detected_by_tool and not (existing.detected_by_tool or "").strip():
        existing.detected_by_tool = detected_by_tool
        existing.detected_by_rule = detected_by_rule
    # Confirmation ledger: every run that filed or re-found this finding,
    # lazily backfilled from the legacy single source_run_uid.
    run_uids = [u for u in (existing.source_run_uids or [existing.source_run_uid]) if u]
    if source_run_uid:
        existing.source_run_uid = source_run_uid
        if source_run_uid not in run_uids:
            run_uids.append(source_run_uid)
    existing.source_run_uids = run_uids
    now = datetime.now(timezone.utc)
    existing.last_confirmed_at = now
    existing.updated_at = now
    await existing.save()


async def create_finding(
    *,
    repository_uid: str,
    title: str,
    tags: Optional[list[str]] = None,
    kind: str = "defect",
    severity: str = "medium",
    size: Optional[str] = None,
    effort: Optional[str] = None,
    subtype: str = "",
    confidence: float = 0.7,
    description: str = "",
    root_cause: str = "",
    why_it_matters: str = "",
    evidence: Optional[dict[str, Any]] = None,
    suggested_fix: str = "",
    affected_paths: Optional[list[str]] = None,
    detected_by_tool: str = "",
    detected_by_rule: str = "",
    source_run_uid: Optional[str] = None,
    executor: str = "manual",
    source_path: str = "tool-call",
    parse_status: str = "ok",
) -> dict[str, Any]:
    """Create a Finding and return its UID and dedupe key.

    `tags` are optional free-text labels ("security", "flaky-test", …) used
    for filtering — never a required taxonomy.

    `size` is the fix-size estimate (trivial | small | medium | large).
    `effort` is a legacy alias for `size` — seeded prompts may still say
    "effort"; it is honoured only when `size` is not explicitly provided.

    The narrative fields (`description`, `root_cause`, `why_it_matters`,
    `suggested_fix`) are rendered to humans as markdown.

    `detected_by_tool`/`detected_by_rule` record static-analysis provenance:
    set them (to the candidate's tool + rule id) when this finding was filed
    after investigating a deterministic analyzer candidate; leave them empty
    for a finding the agent discovered on its own.

    Idempotent: a second call with the same (repo, title, top_path) updates
    the existing Finding's evidence + confidence instead of duplicating.
    When the exact dedupe key misses, a same-kind open finding with an
    overlapping affected path and a near-identical title is merged into
    instead of duplicated (confirmation recorded).
    """
    kind_member = _enum_member(FindingKind, kind, "kind")
    severity_member = _enum_member(Severity, severity, "severity")
    size_member = _enum_member(FindingSize, size if size is not None else (effort or "medium"), "size")
    source_path_member = _enum_member(SourcePath, source_path, "source_path")
    parse_status_member = _enum_member(ParseStatus, parse_status, "parse_status")
    clean_tags = normalize_tags(tags)

    paths = list(affected_paths or [])
    dedupe = build_dedupe_key(
        repository_uid=repository_uid,
        title=title,
        top_path=paths[0] if paths else "",
    )
    existing = await Finding.nodes.get_or_none(dedupe_key=dedupe)
    if existing is not None:
        await _merge_into(
            existing,
            confidence=confidence,
            evidence=evidence,
            clean_tags=clean_tags,
            description=description,
            root_cause=root_cause,
            why_it_matters=why_it_matters,
            suggested_fix=suggested_fix,
            detected_by_tool=detected_by_tool,
            detected_by_rule=detected_by_rule,
            source_run_uid=source_run_uid,
        )
        return {
            "finding_uid": existing.uid,
            "dedupe_key": dedupe,
            "deduplicated": True,
            "note": "duplicate of existing finding; confirmation recorded",
        }

    # Similarity fallback: the dedupe key missed (rephrased title or a
    # different top path), but a same-kind open finding touching one of the
    # same files with a near-identical title is the same issue re-found.
    # Best-effort: a failure here degrades to "maybe file a duplicate" —
    # it must never lose the finding the agent has in hand.
    if paths:
        try:
            # Match on basenames, not full paths: a moved file otherwise
            # defeats BOTH dedupe layers (the key and this fallback), so the
            # same issue forks into a second Finding and splits its cross-run
            # corroboration count.
            incoming_paths = {path_basename(p) for p in paths if p}
            incoming_paths.discard("")
            # "ticketed" counts as live here alongside "open": promotion is
            # not a resolution, so a re-found instance of a finding that
            # already has a ticket is still the SAME issue and must merge into
            # it. Matching only "open" would fork a second node the moment
            # anyone promoted the original.
            candidates = await Finding.nodes.filter(
                repository_uid=repository_uid,
                status__in=[FindingStatus.OPEN.value, FindingStatus.TICKETED.value],
                kind=kind_member.value,
            )
            for candidate in candidates:
                candidate_paths = {
                    path_basename(p) for p in (candidate.affected_paths or []) if p
                }
                if not incoming_paths & candidate_paths:
                    continue
                if not titles_similar(candidate.title or "", title):
                    continue
                await _merge_into(
                    candidate,
                    confidence=confidence,
                    evidence=evidence,
                    clean_tags=clean_tags,
                    description=description,
                    root_cause=root_cause,
                    why_it_matters=why_it_matters,
                    suggested_fix=suggested_fix,
                    detected_by_tool=detected_by_tool,
                    detected_by_rule=detected_by_rule,
                    source_run_uid=source_run_uid,
                )
                return {
                    "finding_uid": candidate.uid,
                    "dedupe_key": candidate.dedupe_key,
                    "deduplicated": True,
                    "note": (
                        "similar open finding matched (same kind, overlapping path, "
                        "near-identical title); confirmation recorded instead of "
                        "creating a duplicate"
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"create_finding: similarity dedup skipped ({type(exc).__name__}: {exc})"
            )

    now = datetime.now(timezone.utc)
    f = Finding(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        tags=clean_tags,
        kind=kind_member.value,
        severity=severity_member.value,
        size=size_member.value,
        subtype=subtype or "",
        title=title,
        confidence=float(confidence),
        description=description,
        root_cause=root_cause,
        why_it_matters=why_it_matters,
        evidence=evidence or {},
        suggested_fix=suggested_fix,
        affected_paths=paths,
        dedupe_key=dedupe,
        detected_by_tool=detected_by_tool or "",
        detected_by_rule=detected_by_rule or "",
        source_run_uid=source_run_uid,
        source_run_uids=[source_run_uid] if source_run_uid else [],
        last_confirmed_at=now,
        executor=executor,
        source_path=source_path_member.value,
        parse_status=parse_status_member.value,
        status=FindingStatus.OPEN.value,
    )
    await f.save()
    await write_audit(
        kind="finding.filed",
        subject_uid=f.uid,
        subject_type="Finding",
        actor_uid=executor,
        payload={
            "tags": list(f.tags or []),
            "kind": f.kind,
            "severity": f.severity,
            "source_run_uid": f.source_run_uid,
            "detected_by_tool": f.detected_by_tool or "",
        },
    )
    return {"finding_uid": f.uid, "dedupe_key": dedupe, "deduplicated": False}
