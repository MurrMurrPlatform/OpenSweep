"""Read tools (OpenSweep data): query existing Findings.

Uses the `opensweep_*` naming convention of the look-before-write contract.
Exposed to envelope and delegated
CLI executors via MCP.

Use cases:
- Before Audit's `create_finding`, the LLM searches for similar open findings
  to avoid duplicates.
- During verify-feature, the LLM looks up the existing open Findings tied to
  the feature's specs.
"""

from __future__ import annotations

from typing import Any, Optional

from domains.findings.models import Finding

# `status` is a plain equality filter, so any value that isn't a real Finding
# status matches nothing. Agents reach for "all" (and the API default makes
# "open" look like one option among several), so an unfiltered listing came
# back EMPTY rather than complete — a silent wrong answer, the worst kind.
# Only "" was accepted as the no-filter escape hatch; spell the obvious
# synonyms too and treat them all as "don't filter on status".
_ANY_STATUS = {"", "all", "any", "*"}


def _status_filter(status: Optional[str]) -> Optional[str]:
    """The status to filter on, or None meaning 'no status filter'."""
    normalized = (status or "").strip().lower()
    return None if normalized in _ANY_STATUS else normalized


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    return {
        "uid": f.uid,
        "repository_uid": f.repository_uid,
        "tags": list(f.tags or []),
        "kind": f.kind,
        "severity": f.severity,
        "subtype": f.subtype,
        "title": f.title,
        "status": f.status,
        "dedupe_key": f.dedupe_key,
        "affected_paths": list(f.affected_paths or []),
        "executor": f.executor,
        "confidence": float(f.confidence or 0.0),
    }


async def opensweep_list_findings(
    *,
    repository_uid: str,
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    status: str = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List Findings in a repository, optionally filtered.

    Defaults to status=open. Pass status="all" (or "", "any", "*") for every
    status. Use BEFORE calling `create_finding` to see if a similar issue is
    already filed — `create_finding` already dedupes on title+top_path, but
    checking here lets the LLM choose to call `update_finding` (adding
    evidence) instead.
    """
    wanted = _status_filter(status)
    nodes = (
        await Finding.nodes.filter(repository_uid=repository_uid, status=wanted)
        if wanted
        else await Finding.nodes.filter(repository_uid=repository_uid)
    )
    out: list[dict[str, Any]] = []
    for f in nodes:
        if tag and tag not in (f.tags or []):
            continue
        if kind and f.kind != kind:
            continue
        out.append(_finding_to_dict(f))
        if len(out) >= limit:
            break
    return out


async def opensweep_search_findings(
    *,
    repository_uid: str,
    query: str,
    status: str = "open",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Substring search across open Finding titles + affected paths.

    Use to ask "did anyone already file a finding about <X>?" before creating
    a new one. Pass status="all" (or "", "any", "*") to search every status.
    """
    needle = (query or "").strip().lower()
    wanted = _status_filter(status)
    nodes = (
        await Finding.nodes.filter(repository_uid=repository_uid, status=wanted)
        if wanted
        else await Finding.nodes.filter(repository_uid=repository_uid)
    )
    out: list[dict[str, Any]] = []
    for f in nodes:
        if needle:
            hay = " ".join(
                [
                    f.title or "",
                    f.subtype or "",
                    " ".join(f.affected_paths or []),
                ]
            ).lower()
            if needle not in hay:
                continue
        out.append(_finding_to_dict(f))
        if len(out) >= limit:
            break
    return out


async def opensweep_get_finding(*, uid: str) -> Optional[dict[str, Any]]:
    """Fetch a single Finding by uid, including evidence."""
    f = await Finding.nodes.get_or_none(uid=uid)
    if f is None:
        return None
    return {
        **_finding_to_dict(f),
        "size": f.size,
        "description": f.description or "",
        "root_cause": f.root_cause or "",
        "why_it_matters": f.why_it_matters or "",
        "evidence": dict(f.evidence or {}),
        "suggested_fix": f.suggested_fix or "",
        "source_run_uid": f.source_run_uid,
        "source_path": f.source_path,
    }
