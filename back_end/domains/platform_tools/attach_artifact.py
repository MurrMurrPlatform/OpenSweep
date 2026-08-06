"""Platform tool: attach_artifact.

Non-patch supporting output: trace summaries, test logs, benchmarks,
screenshots, dependency graphs, reproduction notes, etc. Stored via the
artifact_store and recorded on the target's audit trail.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from infrastructure import artifact_store
from infrastructure.audit import write_audit

# Mirrors the resolver in api/v1/platform_tools.py (_artifact_target_repository_uid).
VALID_TARGET_TYPES = {"run", "finding", "doc", "memory", "ticket", "pull_request", "pullrequest"}

# The tool's target_type vocabulary is lowercase; Neo4j labels are the PascalCase
# neomodel class names and MATCH is case-sensitive. Writing the lowercase value
# as subject_type would file every artifact.attached event under a label that
# matches zero nodes, so every other consumer of the audit stream (and any future
# repository_uid derivation) would see a subject it cannot resolve.
_AUDIT_LABEL = {
    "run": "Run",
    "finding": "Finding",
    "doc": "Doc",
    "memory": "Memory",
    "ticket": "Ticket",
    "pull_request": "PullRequest",
    "pullrequest": "PullRequest",
}


def _artifact_scope_segment(target_type: str, target_uid: str) -> str:
    """The second path segment under `<repo>/…` in the artifact URI.

    A run's artifacts group under the run uid — that's what
    executors/lifecycle already use, and it lets both the executor's raw
    transcript and an agent-attached companion blob live in the same folder.
    For every other target kind the target_uid is NOT a run uid, so we
    namespace by target_type to keep addressing unambiguous (a ticket uid
    could otherwise collide with a run uid in the same repo)."""
    if target_type == "run":
        return target_uid
    return f"{target_type}-{target_uid}"


async def attach_artifact(
    *,
    target_uid: str,
    target_type: str,  # run | finding | doc | memory | ticket | pull_request
    artifact_type: str,
    content: bytes | str,
    repository_uid: Optional[str] = None,
    extension: str = "txt",
    summary: str = "",
    executor: str = "manual",
) -> dict[str, Any]:
    normalized = target_type.strip().lower()
    if normalized not in VALID_TARGET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown target_type '{target_type}' — must be one of: "
                "run, finding, doc, memory, ticket, pull_request"
            ),
        )
    # repository_uid is the tenancy key that the artifacts read route (F8)
    # parses back out of the URI to org-check the request. Silently falling
    # back to target_uid would embed a non-repo uid in the segment used for
    # auth — a cross-org read/write primitive. The HTTP + envelope surfaces
    # both resolve it from the target before calling us; a caller that skips
    # both has no business writing to the store.
    if not (repository_uid or "").strip():
        raise HTTPException(
            status_code=422,
            detail="repository_uid is required (resolve it from the target before calling attach_artifact)",
        )
    uri = artifact_store.put(
        repository_uid=repository_uid,
        run_uid=_artifact_scope_segment(normalized, target_uid),
        content=content,
        artifact_type=artifact_type,
        extension=extension,
        summary=summary,
    )
    # Pass repository_uid explicitly rather than letting write_audit derive it:
    # we already hold the authoritative, caller-resolved tenancy key, and the
    # derivation would otherwise cost a Cypher round-trip per attach.
    await write_audit(
        kind="artifact.attached",
        subject_uid=target_uid,
        subject_type=_AUDIT_LABEL[normalized],
        actor_uid=executor,
        repository_uid=repository_uid,
        payload={
            "artifact_type": artifact_type,
            "artifact_ref": uri,
            "summary": summary,
        },
    )
    return {"target_uid": target_uid, "artifact_ref": uri}
