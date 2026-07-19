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
