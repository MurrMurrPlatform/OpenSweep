"""Read tool (internal_llm only): query existing Findings.

Used by internal_llm to avoid re-discovering work that has already been
recorded by past Runs.
"""

from __future__ import annotations

from typing import Any, Optional

from domains.findings.models import Finding


async def prior_findings(
    *,
    repository_uid: str,
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    # "Already recorded" includes findings someone has since opened a ticket
    # for — a ticket in flight is the strongest possible reason not to
    # re-discover something, so "ticketed" has to be in the default set.
    statuses: tuple[str, ...] = ("open", "acknowledged", "ticketed"),
    title_substring: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    nodes = (
        await Finding.nodes.filter(
            repository_uid=repository_uid, status__in=list(statuses)
        )
        if statuses
        else await Finding.nodes.filter(repository_uid=repository_uid)
    )
    out: list[dict[str, Any]] = []
    needle = (title_substring or "").lower()
    for f in nodes:
        if tag and tag not in (f.tags or []):
            continue
        if kind and f.kind != kind:
            continue
        if needle and needle not in (f.title or "").lower():
            continue
        out.append(
            {
                "uid": f.uid,
                "tags": list(f.tags or []),
                "kind": f.kind,
                "severity": f.severity,
                "subtype": f.subtype,
                "title": f.title,
                "status": f.status,
                "executor": f.executor,
                "dedupe_key": f.dedupe_key,
            }
        )
        if len(out) >= limit:
            break
    return out
