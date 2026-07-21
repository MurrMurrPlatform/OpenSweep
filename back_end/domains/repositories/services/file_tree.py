"""Repository file tree — the shared "what files exist right now" primitive.

Campaign planning and the area detail view both size their answers against
the default branch's blob paths; this module owns that lookup so neither
domain imports the other for it.
"""

from __future__ import annotations

from logging_config import logger


async def file_tree_paths(repo) -> tuple[list[str], str]:
    """(blob paths at the default branch head, degraded_reason — "" = full).

    Empty paths + a reason on ANY failure (missing provider, no head sha,
    network) — callers degrade to watch-path-only answers instead of
    failing. A truncated tree (very large repo) keeps the partial paths but
    still carries a reason: file counts and any remainder are computed
    against a partial universe, and the caller must say so."""
    from infrastructure.git_providers import get_provider_client

    try:
        client = get_provider_client(repo)
        if not (client.is_active and repo.github_owner and repo.github_repo):
            logger.warning(
                f"file tree: no active git provider for {repo.uid} — "
                "degrading to watch paths only",
                extra={"tag": "repositories"},
            )
            return [], "no active git provider connection"
        sha = await client.get_branch_head_sha(
            repo.github_owner, repo.github_repo, repo.default_branch or "main"
        )
        if not sha:
            logger.warning(
                f"file tree: no head sha for {repo.uid} — "
                "degrading to watch paths only",
                extra={"tag": "repositories"},
            )
            return [], f"no head sha for branch {repo.default_branch or 'main'}"
        tree = await client.get_tree(repo.github_owner, repo.github_repo, sha)
        paths = [str(p) for p in (tree.get("paths") or [])]
        if tree.get("truncated"):
            logger.warning(
                f"file tree: truncated for {repo.uid} "
                f"({len(paths)} paths) — partial file list",
                extra={"tag": "repositories"},
            )
            return paths, "file tree truncated (very large repo) — partial file list"
        return paths, ""
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the caller
        logger.warning(
            f"file tree: unavailable for {repo.uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "repositories"},
        )
        return [], f"file tree unavailable ({type(exc).__name__})"
