"""Default-branch freshness sync — the one place changed paths are resolved.

Two entry points, one cursor:

- `sync_push_freshness` — the realtime path. A `push` webhook to the default
  branch resolves its true changed paths and feeds them to the stale-markers.
- `reconcile_repository_freshness` — the safety net. Compares the persisted
  cursor against the live default-branch head and replays whatever the webhook
  never delivered.

Both advance `Repository.freshness_synced_sha`, so they are idempotent with
each other: whichever runs second sees a cursor already at (or past) the head
and does nothing. Without the second path, a single dropped delivery leaves
every area reading "fresh" forever with nothing to notice or repair it.

Degradation is explicit, never silent. When the changed-path list is partial
(compare truncated, provider down, per-node save failures) the reason is
persisted to `freshness_degraded_reason` and surfaced in the UI, because a
partial stale-marking is indistinguishable from a clean board otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from domains.repositories.services.push_paths import (
    PushPaths,
    changed_paths_from_push,
)
from logging_config import logger


@dataclass
class FreshnessSyncResult:
    """Outcome of one sync attempt against one repository."""

    repository_uid: str = ""
    # False when the trigger was irrelevant (non-default branch, no movement).
    applied: bool = False
    reason: str = ""
    changed_paths: int = 0
    docs_marked: int = 0
    areas_marked: int = 0
    head_sha: str = ""
    degraded_reason: str = ""
    errors: list[str] = field(default_factory=list)


async def _resolve_range_paths(repo, base: str, head: str) -> tuple[list[str], str]:
    """(changed paths between two commits, degraded_reason — "" = complete).

    Degrades to ([], reason) on any provider failure so callers can fall back
    to whatever the payload gave them, mirroring `file_tree_paths`.
    """
    from infrastructure.git_providers import get_provider_client

    try:
        client = get_provider_client(repo)
        if not (client.is_active and repo.github_owner and repo.github_repo):
            return [], "no active git provider connection"
        result = await client.compare_commits(
            repo.github_owner, repo.github_repo, base, head
        )
        paths = [str(p) for p in (result.get("paths") or [])]
        if result.get("truncated"):
            return paths, (
                f"compare {base[:7]}..{head[:7]} truncated at 300 files — "
                "some areas may not be marked stale"
            )
        return paths, ""
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the caller
        logger.warning(
            f"freshness sync: compare failed for {repo.uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "freshness"},
        )
        return [], f"compare unavailable ({type(exc).__name__})"


async def _apply(
    repo,
    *,
    changed_paths: list[str],
    head_sha: str,
    source: str,
    degraded_reason: str = "",
) -> FreshnessSyncResult:
    """Mark stale from `changed_paths`, then advance the cursor."""
    from domains.agents.services.event_triggers import refresh_docs_for_change

    result = FreshnessSyncResult(
        repository_uid=repo.uid,
        applied=True,
        changed_paths=len(changed_paths),
        head_sha=head_sha,
    )
    if changed_paths:
        refreshed = await refresh_docs_for_change(
            repository_uid=repo.uid, changed_paths=changed_paths, source=source
        )
        result.docs_marked = refreshed.docs_marked
        result.areas_marked = refreshed.areas_marked
        result.errors = list(refreshed.errors)
        degraded_reason = degraded_reason or refreshed.degraded_reason

    result.degraded_reason = degraded_reason
    await _stamp_cursor(repo, head_sha, degraded_reason)
    return result


async def _stamp_cursor(repo, head_sha: str, degraded_reason: str) -> None:
    """Advance the freshness cursor. Best-effort: a cursor that fails to save
    only costs a replay on the next tick, which is harmless (marking stale is
    idempotent) — far better than failing the webhook."""
    try:
        if head_sha:
            repo.freshness_synced_sha = head_sha
        repo.freshness_synced_at = datetime.now(UTC)
        repo.freshness_degraded_reason = degraded_reason
        await repo.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"freshness sync: cursor save failed for {repo.uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "freshness"},
        )


async def sync_push_freshness(repo, payload: dict) -> FreshnessSyncResult:
    """Handle one `push` webhook payload for one repository.

    Pushes to any branch other than the default are ignored outright: a
    feature branch is not yet true of the codebase, so marking areas stale
    from it makes the badge mean "someone pushed something somewhere".
    """
    pushed: PushPaths = changed_paths_from_push(
        payload, repo.default_branch or "main"
    )
    if not pushed.accepted:
        return FreshnessSyncResult(repository_uid=repo.uid, reason=pushed.reason)

    degraded = ""
    if pushed.comparable:
        paths, degraded = await _resolve_range_paths(repo, pushed.before, pushed.after)
        if not paths and degraded:
            # Compare unavailable — fall back to the payload's own commit
            # array, which is capped at 20 commits and may be incomplete.
            paths = list(pushed.payload_paths)
            if pushed.commits_truncated:
                degraded = (
                    f"{degraded}; fell back to a truncated push payload "
                    "(>20 commits) — some areas may not be marked stale"
                )
    else:
        # Branch creation (or a payload without a usable range): the commit
        # array is all we have.
        paths = list(pushed.payload_paths)
        degraded = (
            "push payload truncated at 20 commits — some areas may not be "
            "marked stale"
            if pushed.commits_truncated
            else ""
        )

    return await _apply(
        repo,
        changed_paths=paths,
        head_sha=pushed.after,
        source="webhook",
        degraded_reason=degraded,
    )


async def reconcile_repository_freshness(repo) -> FreshnessSyncResult:
    """Replay any default-branch movement the webhook path never delivered.

    A repository whose cursor is empty (just registered, or migrated in)
    ADOPTS the current head without marking anything stale — otherwise every
    area would be born stale against the repo's entire history, which says
    nothing about whether the map is still accurate.
    """
    from infrastructure.git_providers import get_provider_client

    branch = repo.default_branch or "main"
    try:
        client = get_provider_client(repo)
        if not (client.is_active and repo.github_owner and repo.github_repo):
            return FreshnessSyncResult(
                repository_uid=repo.uid, reason="no active git provider connection"
            )
        head = await client.get_branch_head_sha(
            repo.github_owner, repo.github_repo, branch
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"freshness reconcile: head lookup failed for {repo.uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "freshness"},
        )
        return FreshnessSyncResult(
            repository_uid=repo.uid, reason=f"head unavailable ({type(exc).__name__})"
        )

    if not head:
        return FreshnessSyncResult(
            repository_uid=repo.uid, reason=f"no head sha for branch {branch}"
        )

    cursor = (repo.freshness_synced_sha or "").strip()
    if cursor == head:
        return FreshnessSyncResult(
            repository_uid=repo.uid, head_sha=head, reason="already at head"
        )

    if not cursor:
        # First sight of this repo — adopt the head, mark nothing.
        await _stamp_cursor(repo, head, "")
        return FreshnessSyncResult(
            repository_uid=repo.uid,
            head_sha=head,
            reason="adopted current head (first freshness sync)",
        )

    paths, degraded = await _resolve_range_paths(repo, cursor, head)
    if not paths and degraded:
        # Do NOT advance the cursor on a failed compare — leaving it behind is
        # what lets the next tick retry the same range.
        logger.warning(
            f"freshness reconcile: {repo.uid} could not resolve {cursor[:7]}.."
            f"{head[:7]}: {degraded}",
            extra={"tag": "freshness"},
        )
        return FreshnessSyncResult(
            repository_uid=repo.uid, head_sha=head, reason=degraded,
            degraded_reason=degraded,
        )

    result = await _apply(
        repo,
        changed_paths=paths,
        head_sha=head,
        source="reconcile",
        degraded_reason=degraded,
    )
    if result.docs_marked or result.areas_marked:
        logger.info(
            f"freshness reconcile: {repo.uid} caught up {cursor[:7]}..{head[:7]} "
            f"({result.docs_marked} docs, {result.areas_marked} areas marked stale)",
            extra={"tag": "freshness"},
        )
    return result


async def reconcile_all_repositories() -> dict:
    """Beat-tick entry: reconcile every active repository. Best-effort per
    repo — one broken connection never blocks the rest."""
    from domains.repositories.models import Repository

    repos = await Repository.nodes.filter(is_active=True)
    applied = 0
    docs = 0
    areas = 0
    for repo in repos:
        try:
            result = await reconcile_repository_freshness(repo)
            if result.applied:
                applied += 1
                docs += result.docs_marked
                areas += result.areas_marked
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"freshness reconcile: {getattr(repo, 'uid', '?')} failed: "
                f"{type(exc).__name__}: {exc}",
                extra={"tag": "freshness"},
            )
    return {
        "repositories": len(repos),
        "applied": applied,
        "docs_marked": docs,
        "areas_marked": areas,
    }
