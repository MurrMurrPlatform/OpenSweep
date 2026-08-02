"""Structural protocol for git-provider clients.

`GitProviderClient` is the read/write API surface OpenSweep actually uses against
a git hosting provider. Signatures are copied verbatim from
`infrastructure.github_client.GitHubClient` — the GitHub implementation —
so it conforms structurally with zero changes. `owner`/`repo` are the
provider's namespace/name coordinates; payloads are the provider's raw dicts
(today: GitHub REST shapes).

Only methods with real call sites belong here — do not add speculative ones.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GitProviderClient(Protocol):
    """What a provider client must offer. Today only GitHubClient implements
    it; the protocol exists so call sites stop naming GitHub directly."""

    @property
    def is_active(self) -> bool:
        """True when the client holds (or can resolve) a credential."""
        ...

    async def get_file_contents(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> Any:
        """File payload dict, or a list for a directory listing (trace.py
        relies on the list form)."""
        ...

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any] | None:
        """Branch payload, or None when the branch does not exist."""
        ...

    async def get_branch_head_sha(self, owner: str, repo: str, branch: str) -> str: ...

    async def compare_commits(
        self, owner: str, repo: str, base: str, head: str
    ) -> dict[str, Any]:
        """{"paths": [changed file paths], "truncated": bool} between two
        commits — the freshness path's changed-path source, since push payloads
        cap `commits` at 20 and merge commits carry empty file lists."""
        ...

    async def get_tree(
        self, owner: str, repo: str, tree_sha: str, *, recursive: bool = True
    ) -> dict[str, Any]:
        """{"paths": [blob paths], "truncated": bool} for the tree at
        `tree_sha` — the repo-wide file listing area planning slices up."""
        ...

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...

    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """Changed files of a PR incl. per-file unified patches (the PR
        Files panel)."""
        ...

    async def list_check_runs(
        self, owner: str, repo: str, ref: str = "HEAD"
    ) -> list[dict[str, Any]]:
        """CI rollup at `ref`. Capability-optional: providers without check
        runs may return []."""
        ...

    async def open_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> dict[str, Any]: ...

    async def mark_pull_request_ready(self, owner: str, repo: str, number: int) -> None:
        """Flip a draft PR to ready-for-review; no-op when already ready."""
        ...

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        method: str = "squash",
        commit_title: str = "",
        commit_message: str = "",
    ) -> dict[str, Any]:
        """Merge a PR. Returns {"merged": bool, "sha": str, "message": str}; when
        the provider refuses (not mergeable / head moved) "merged" is False and
        "message" carries the reason — callers surface it, never raise raw."""
        ...

    async def check_write_access(self, owner: str, repo: str) -> bool | None:
        """Whether the bound credential may push. True/False, or None when the
        provider cannot tell — callers block only on an explicit False.

        Capability-optional: a provider with no cheap way to ask may return
        None unconditionally, which restores the pre-preflight behavior of
        discovering the answer at push time.
        """
        ...

    async def create_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str = "",
        target_url: str = "",
    ) -> dict[str, Any]: ...

    async def create_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """Post a plain conversation comment on a pull request."""
        ...

    async def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        body: str,
        event: str = "COMMENT",
        commit_id: str = "",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Publish a review: a summary body plus optional inline comments,
        each {"path", "line", "side", "body"}."""
        ...
