"""What a GitHub `push` payload actually says about changed files — pure.

Freshness hangs off this: an Area or Doc page is stale when code moved under
its scope since the last review, and "code moved" means **the default branch
moved**. Two things the naive reading of a push payload gets wrong:

1. A push to any branch is not a change to the codebase. Work on a feature
   branch is not yet true of `main`, so marking areas stale from it makes the
   stale badge mean "someone pushed something somewhere". Only pushes to the
   repository's default branch count.
2. `payload["commits"]` is capped at 20 commits by GitHub, and merge commits
   carry empty `added`/`modified`/`removed` lists. Merging a 30-commit PR
   therefore reports a fraction of what changed — silently. The payload's
   commit array is a *fallback*, never the primary source; the caller resolves
   `before...after` through the compare API instead.

Kept free of I/O so the decision is unit-testable without a webhook or a
GitHub client (same split as `delivery_disposition` in the webhook router).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# GitHub sends an all-zero sha for the "before" side of a branch creation and
# the "after" side of a branch deletion.
NULL_SHA = "0" * 40

# GitHub caps the push payload's commit array at this many entries.
PAYLOAD_COMMIT_CAP = 20


@dataclass(frozen=True)
class PushPaths:
    """The freshness-relevant reading of one push payload."""

    # False → this push must not touch freshness at all; `reason` says why.
    accepted: bool = False
    reason: str = ""
    branch: str = ""
    before: str = ""
    after: str = ""
    # Branch creation/deletion: there is no usable `before..after` range.
    created: bool = False
    deleted: bool = False
    # Paths recovered from payload["commits"] — the fallback when compare is
    # unavailable. Possibly incomplete; see `commits_truncated`.
    payload_paths: list[str] = field(default_factory=list)
    # payload["commits"] hit GitHub's cap, so `payload_paths` is definitely
    # missing changes. Only meaningful when the caller falls back to it.
    commits_truncated: bool = False

    @property
    def comparable(self) -> bool:
        """True when `before...after` is a range the compare API can resolve."""
        return bool(
            self.accepted
            and not self.created
            and not self.deleted
            and self.before
            and self.after
            and self.before != NULL_SHA
            and self.after != NULL_SHA
        )


def changed_paths_from_push(payload: dict[str, Any], default_branch: str) -> PushPaths:
    """Read a `push` payload for freshness purposes.

    Rejects (accepted=False) anything that is not a commit-bearing push to
    `default_branch`: tags, other branches, and branch deletions. Never raises
    — a malformed payload is a rejection, not an exception, because the webhook
    must still return 200.
    """
    if not isinstance(payload, dict):
        return PushPaths(reason="payload is not an object")

    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/"):
        # Tags and other non-branch refs never move the codebase.
        return PushPaths(reason=f"ref {ref!r} is not a branch")
    branch = ref.removeprefix("refs/heads/")
    if not branch:
        return PushPaths(reason="empty branch name")

    wanted = (default_branch or "main").strip()
    if branch != wanted:
        return PushPaths(
            branch=branch,
            reason=f"push to {branch!r}, not the default branch {wanted!r}",
        )

    deleted = bool(payload.get("deleted"))
    if deleted:
        # The branch is gone; nothing to compare and nothing changed *in* it.
        return PushPaths(
            branch=branch, deleted=True, reason=f"branch {branch!r} deleted"
        )

    commits = payload.get("commits")
    commits = commits if isinstance(commits, list) else []
    paths: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified", "removed"):
            entries = commit.get(key)
            if isinstance(entries, list):
                paths.extend(str(p) for p in entries if p)

    return PushPaths(
        accepted=True,
        branch=branch,
        before=str(payload.get("before") or ""),
        after=str(payload.get("after") or ""),
        created=bool(payload.get("created")) or str(payload.get("before") or "") == NULL_SHA,
        payload_paths=list(dict.fromkeys(paths)),
        commits_truncated=len(commits) >= PAYLOAD_COMMIT_CAP,
    )
