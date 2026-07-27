"""Finding service -- list, get, file, dismiss, acknowledge, mark fixed.

Status transitions on the Finding cover the v1 tracking lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.findings.models import Finding
from domains.findings.schemas import (
    FileFindingRequest,
    FindingDTO,
    FindingKind,
    FindingSize,
    FindingStatus,
    ParseStatus,
    Severity,
    SourcePath,
    UpdateFindingRequest,
    normalize_tags,
)
from domains.findings.services.dedupe import build_dedupe_key
from domains.findings.services.trust import trust_score_for
from domains.runs.services.run_provider import provider_info_for_run
from infrastructure.audit import write_audit

# Imported, not redefined: this ladder had three divergent copies (here,
# delivery.convergence, tickets.ticket_service). Re-exported because callers
# and tests already import `severity_rank` from this module.
from infrastructure.ranking import severity_rank  # noqa: F401

FINDING_SORT_FIELDS = frozenset(
    {"updated_at", "created_at", "severity", "confidence", "title", "trust"}
)
FINDING_SORT_DIRS = frozenset({"asc", "desc"})


def sort_findings(
    items: list[FindingDTO], *, sort_by: str = "updated_at", sort_dir: str = "desc"
) -> list[FindingDTO]:
    """Sort finding DTOs by a whitelisted field; recency breaks ties."""
    floor = datetime.min.replace(tzinfo=UTC)

    def recency(f: FindingDTO) -> datetime:
        return f.updated_at or f.created_at or floor

    reverse = sort_dir != "asc"
    if sort_by == "created_at":
        key = lambda f: f.created_at or floor  # noqa: E731
    elif sort_by == "severity":
        key = lambda f: (severity_rank(f.severity.value), recency(f))  # noqa: E731
    elif sort_by == "confidence":
        key = lambda f: (f.confidence, recency(f))  # noqa: E731
    elif sort_by == "trust":
        # Severity breaks trust ties: of two equally-credible findings the
        # worse one should be looked at first.
        key = lambda f: (f.trust, severity_rank(f.severity.value), recency(f))  # noqa: E731
    elif sort_by == "title":
        key = lambda f: (f.title or "").casefold()  # noqa: E731
        reverse = sort_dir == "desc"
    else:  # updated_at (default)
        key = recency  # noqa: E731
    return sorted(items, key=key, reverse=reverse)


def finding_to_dto(f: Finding) -> FindingDTO:
    return FindingDTO(
        uid=f.uid,
        repository_uid=f.repository_uid,
        tags=list(f.tags or []),
        kind=FindingKind(f.kind),
        severity=Severity(f.severity or "medium"),
        size=FindingSize(f.size or "medium"),
        subtype=f.subtype or "",
        title=f.title,
        confidence=float(f.confidence or 0.7),
        description=f.description or "",
        root_cause=f.root_cause or "",
        why_it_matters=f.why_it_matters or "",
        evidence=dict(f.evidence or {}),
        suggested_fix=f.suggested_fix or "",
        affected_paths=list(f.affected_paths or []),
        dedupe_key=f.dedupe_key,
        source_run_uid=f.source_run_uid,
        source_run_uids=list(f.source_run_uids or ([f.source_run_uid] if f.source_run_uid else [])),
        last_confirmed_at=f.last_confirmed_at,
        executor=f.executor or "manual",
        source_path=SourcePath(f.source_path or "tool-call"),
        parse_status=ParseStatus(f.parse_status or "ok"),
        detected_by_tool=f.detected_by_tool or "",
        detected_by_rule=f.detected_by_rule or "",
        # Derived, never stored: computing it at the DTO boundary means it
        # cannot drift from the signals behind it (services/trust.py).
        trust=trust_score_for(f),
        status=FindingStatus(f.status or "open"),
        created_at=f.created_at,
        updated_at=f.updated_at,
    )


class FindingService:
    async def list(
        self,
        *,
        repository_uid: str | None = None,
        source_run_uid: str | None = None,
        tag: str | None = None,
        kind: str | None = None,
        exclude_kind: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        size: str | None = None,
        detected_by_tool: str | None = None,
        sort_by: str = "updated_at",
        sort_dir: str = "desc",
    ) -> list[FindingDTO]:
        nodes = (
            await Finding.nodes.filter(repository_uid=repository_uid)
            if repository_uid
            # intentional: repository_uid is optional here; without it this is a
            # deliberate cross-repo listing (admin surfaces), no tenant column to push.
            else await Finding.nodes.all()
        )
        out = []
        for f in nodes:
            if source_run_uid and f.source_run_uid != source_run_uid:
                continue
            if tag and tag not in (f.tags or []):
                continue
            if kind and f.kind != kind:
                continue
            if exclude_kind and f.kind == exclude_kind:
                continue
            if status and f.status != status:
                continue
            if severity and (f.severity or "medium") != severity:
                continue
            if size and (f.size or "medium") != size:
                continue
            if detected_by_tool and (f.detected_by_tool or "") != detected_by_tool:
                continue
            out.append(finding_to_dto(f))
        return sort_findings(out, sort_by=sort_by, sort_dir=sort_dir)

    async def get(self, uid: str) -> FindingDTO:
        f = await Finding.nodes.get_or_none(uid=uid)
        if f is None:
            raise HTTPException(status_code=404, detail=f"Finding {uid} not found")
        dto = finding_to_dto(f)
        provider = await provider_info_for_run(f.source_run_uid)
        dto.provider_uid = provider.uid
        dto.provider_label = provider.label
        dto.provider_kind = provider.kind
        dto.provider_model = provider.model
        return dto

    async def get_node(self, uid: str) -> Finding:
        f = await Finding.nodes.get_or_none(uid=uid)
        if f is None:
            raise HTTPException(status_code=404, detail=f"Finding {uid} not found")
        return f

    async def file_finding(
        self, req: FileFindingRequest, *, actor_uid: str | None = None
    ) -> FindingDTO:
        dedupe = build_dedupe_key(
            repository_uid=req.repository_uid,
            title=req.title,
            top_path=(req.affected_paths or [""])[0],
        )
        existing = await Finding.nodes.get_or_none(dedupe_key=dedupe)
        if existing is not None:
            existing.confidence = max(float(existing.confidence or 0.0), req.confidence)
            existing.evidence = {**(existing.evidence or {}), **(req.evidence or {})}
            # Confirmation ledger — keep this path in step with the platform
            # tool's _merge_into (create_finding.py).
            run_uids = [u for u in (existing.source_run_uids or []) if u]
            if not run_uids and existing.source_run_uid:
                run_uids = [existing.source_run_uid]
            if req.source_run_uid and req.source_run_uid not in run_uids:
                run_uids.append(req.source_run_uid)
            existing.source_run_uids = run_uids
            existing.last_confirmed_at = datetime.now(UTC)
            existing.updated_at = datetime.now(UTC)
            await existing.save()
            return finding_to_dto(existing)

        f = Finding(
            uid=uuid4().hex,
            repository_uid=req.repository_uid,
            tags=normalize_tags(req.tags),
            kind=req.kind.value,
            severity=req.severity.value,
            size=req.size.value,
            subtype=req.subtype or "",
            title=req.title,
            confidence=req.confidence,
            description=req.description,
            root_cause=req.root_cause,
            why_it_matters=req.why_it_matters,
            evidence=req.evidence or {},
            suggested_fix=req.suggested_fix,
            affected_paths=req.affected_paths,
            dedupe_key=dedupe,
            detected_by_tool=req.detected_by_tool or "",
            detected_by_rule=req.detected_by_rule or "",
            source_run_uid=req.source_run_uid,
            source_run_uids=[req.source_run_uid] if req.source_run_uid else [],
            last_confirmed_at=datetime.now(UTC),
            executor=req.executor or "manual",
            source_path=SourcePath.TOOL_CALL.value,
            parse_status=ParseStatus.OK.value,
            status=FindingStatus.OPEN.value,
        )
        await f.save()
        await write_audit(
            kind="finding.filed",
            subject_uid=f.uid,
            subject_type="Finding",
            actor_uid=actor_uid,
            payload={"tags": list(f.tags or []), "severity": f.severity, "kind": f.kind},
        )
        return finding_to_dto(f)

    async def update(
        self, uid: str, req: UpdateFindingRequest, *, actor_uid: str | None = None
    ) -> FindingDTO:
        f = await self.get_node(uid)
        fields = req.model_dump(exclude_unset=True)
        # dedupe_key is intentionally left untouched — it pins the finding's
        # identity for merge-on-refile; a human title tweak shouldn't spawn a
        # duplicate or collide with another finding's unique key.
        for key, value in fields.items():
            if key == "tags":
                value = normalize_tags(value)
            elif key in ("kind", "severity", "size") and value is not None:
                value = value.value  # StrEnum → stored string
            setattr(f, key, value)
        f.updated_at = datetime.now(UTC)
        await f.save()
        await write_audit(
            kind="finding.edited",
            subject_uid=uid,
            subject_type="Finding",
            actor_uid=actor_uid,
            payload={"fields": sorted(fields.keys())},
        )
        return finding_to_dto(f)

    async def _set_status(
        self, uid: str, status: FindingStatus, *, actor_uid: str | None = None, audit_kind: str
    ) -> FindingDTO:
        f = await self.get_node(uid)
        f.status = status.value
        f.updated_at = datetime.now(UTC)
        await f.save()
        await write_audit(
            kind=audit_kind, subject_uid=uid, subject_type="Finding", actor_uid=actor_uid
        )
        # Finding status is a convergence input for any PR holding a
        # resolution of this finding — refresh those ledgers (best-effort).
        try:
            from domains.delivery.services.pull_request_service import (
                recompute_open_prs_for_finding,
            )

            await recompute_open_prs_for_finding(uid)
        except Exception:  # noqa: BLE001 — never fail the status change
            from logging_config import logger

            logger.warning(
                f"post-status PR recompute failed for finding {uid}",
                extra={"tag": "delivery"},
                exc_info=True,
            )
        return finding_to_dto(f)

    async def dismiss(self, uid: str, *, actor_uid: str | None = None) -> FindingDTO:
        return await self._set_status(
            uid, FindingStatus.DISMISSED, actor_uid=actor_uid, audit_kind="finding.dismissed"
        )

    async def acknowledge(self, uid: str, *, actor_uid: str | None = None) -> FindingDTO:
        return await self._set_status(
            uid, FindingStatus.ACKNOWLEDGED, actor_uid=actor_uid, audit_kind="finding.acknowledged"
        )

    async def wont_fix(self, uid: str, *, actor_uid: str | None = None) -> FindingDTO:
        return await self._set_status(
            uid, FindingStatus.WONT_FIX, actor_uid=actor_uid, audit_kind="finding.wont_fix"
        )

    async def mark_fixed(self, uid: str, *, actor_uid: str | None = None) -> FindingDTO:
        return await self._set_status(
            uid, FindingStatus.FIXED, actor_uid=actor_uid, audit_kind="finding.fixed"
        )

    async def delete(self, uid: str, *, actor_uid: str | None = None) -> None:
        f = await self.get_node(uid)
        await f.delete()
        await write_audit(
            kind="finding.deleted",
            subject_uid=uid,
            subject_type="Finding",
            actor_uid=actor_uid,
        )

    async def delete_many(self, uids: list[str], *, actor_uid: str | None = None) -> dict[str, int]:
        deleted = 0
        missing = 0
        for uid in list(dict.fromkeys(uids or [])):
            f = await Finding.nodes.get_or_none(uid=uid)
            if f is None:
                missing += 1
                continue
            await f.delete()
            deleted += 1
            await write_audit(
                kind="finding.deleted",
                subject_uid=uid,
                subject_type="Finding",
                actor_uid=actor_uid,
                payload={"bulk": True},
            )
        return {"deleted": deleted, "missing": missing}
