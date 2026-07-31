"""Minimal async GitHub client using httpx.

Auth seam: a static Personal Access Token (the original path — unchanged) OR
a `TokenSource` that resolves the credential per request (GitHub App
installation tokens are short-lived, so they cannot be baked into the
client's default headers). When neither is configured, `is_active` is False
and callers should fall back to the mock store. The client is intentionally
small — it only implements the endpoints we actually use.
"""

import asyncio
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from config import settings
from logging_config import logger

_GITHUB_API = "https://api.github.com"

# Defensive upper bound when paginating check-run rollups — a rollup this
# large is pathological and we only need enough to compute red/pending/green.
MAX_CHECK_RUNS = 500

# Defensive upper bound when paginating a PR's changed files (GitHub itself
# stops listing at 3000) — enough for any reviewable diff.
MAX_PR_FILES = 1000

# GitHub's compare endpoint returns at most 300 changed files and does NOT
# paginate them (only the commit list pages). Past that the path list is
# partial and callers must degrade loudly rather than assume "nothing else
# changed".
MAX_COMPARE_FILES = 300


class TokenSource(Protocol):
    """Per-request credential resolver (e.g. an App installation token)."""

    async def get_token(self) -> str: ...


class MissingCredentialError(RuntimeError):
    """A token source resolved to an empty credential.

    Raised instead of sending `Authorization: Bearer ` (empty token), which
    httpx rejects as LocalProtocolError("Illegal header value") — an error
    that names the transport instead of the actual problem (e.g. the repo's
    GitConnection was deleted and no fallback PAT is configured)."""


class GitHubClient:
    def __init__(self, token: str | None = None, *, token_source: TokenSource | None = None) -> None:
        # A token_source takes precedence; the PAT path is byte-for-byte the
        # old behavior (static Authorization header on the httpx client).
        self._token_source = token_source
        self._token = "" if token_source is not None else (token if token is not None else settings.GITHUB_TOKEN)
        self._client = httpx.AsyncClient(
            base_url=_GITHUB_API,
            timeout=15,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )

    @property
    def is_active(self) -> bool:
        return bool(self._token) or self._token_source is not None

    async def _request_headers(self) -> dict[str, str] | None:
        """Per-request header override — only the token-source path needs one."""
        if self._token_source is None:
            return None
        token = await self._token_source.get_token()
        if not token:
            raise MissingCredentialError(
                "git credential resolved to empty — the repo's connection is "
                "gone or unreadable and no fallback PAT is configured"
            )
        return {"Authorization": f"Bearer {token}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        """GET /repos/{owner}/{repo} — metadata incl. default_branch."""
        return await self._get(f"/repos/{owner}/{repo}")

    async def list_repositories_for_owner(self, owner: str) -> list[dict[str, Any]]:
        return await self._get(f"/users/{owner}/repos")

    async def list_open_issues(self, owner: str, repo: str) -> list[dict[str, Any]]:
        return await self._get(f"/repos/{owner}/{repo}/issues?state=open&per_page=50")

    async def list_check_runs(self, owner: str, repo: str, ref: str = "HEAD") -> list[dict[str, Any]]:
        """Full CI rollup at `ref` — paginated (per_page=100, Link rel="next")
        so rollups larger than one page aren't truncated, `filter=latest` so
        re-run check suites don't double-count, capped at MAX_CHECK_RUNS."""
        if not self.is_active:
            raise RuntimeError("GitHubClient is not active (GITHUB_TOKEN unset)")
        headers = await self._request_headers()
        url: str | None = (
            f"/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100&filter=latest"
        )
        runs: list[dict[str, Any]] = []
        while url and len(runs) < MAX_CHECK_RUNS:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            body = r.json()
            runs.extend(body.get("check_runs", []) if isinstance(body, dict) else [])
            url = (r.links.get("next") or {}).get("url")
        return runs[:MAX_CHECK_RUNS]

    async def list_dependabot_alerts(self, owner: str, repo: str) -> list[dict[str, Any]]:
        # Requires the token to have `security_events` scope or repo admin.
        return await self._get(f"/repos/{owner}/{repo}/dependabot/alerts?state=open")

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return await self._get(f"/repos/{owner}/{repo}/pulls/{number}")

    async def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[dict[str, Any]]:
        return await self._get(f"/repos/{owner}/{repo}/pulls?state={state}&per_page=50")

    async def list_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Changed files of a PR with per-file unified patches — paginated
        (per_page=100, Link rel="next"), capped at MAX_PR_FILES. GitHub omits
        `patch` for binary and oversized diffs."""
        if not self.is_active:
            raise RuntimeError("GitHubClient is not active (GITHUB_TOKEN unset)")
        headers = await self._request_headers()
        url: str | None = f"/repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
        files: list[dict[str, Any]] = []
        while url and len(files) < MAX_PR_FILES:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            body = r.json()
            files.extend(body if isinstance(body, list) else [])
            url = (r.links.get("next") or {}).get("url")
        return files[:MAX_PR_FILES]

    async def get_file_contents(self, owner: str, repo: str, path: str, ref: str | None = None) -> dict[str, Any]:
        # Reject path traversal and percent-encode both the path and ref so a
        # crafted value (e.g. "../../other-owner/other-repo/contents/x") can't
        # escape this repo's contents endpoint and reach anything the token can
        # read. `safe="/"` keeps sub-directory separators intact.
        clean = path.strip("/")
        if ".." in clean.split("/"):
            raise ValueError(f"invalid path: {path!r}")
        url = f"/repos/{owner}/{repo}/contents/{quote(clean, safe='/')}"
        if ref:
            url = f"{url}?ref={quote(ref, safe='')}"
        return await self._get(url)

    async def get_tree(
        self, owner: str, repo: str, tree_sha: str, *, recursive: bool = True
    ) -> dict[str, Any]:
        """GET /repos/{o}/{r}/git/trees/{sha} — blob paths of the whole tree.

        `recursive=1` flattens the tree in one call; GitHub truncates
        pathological trees (~100k entries) and flags it via `truncated`,
        surfaced verbatim so callers can fall back to a clone-side listing.
        Only blob (file) paths are returned — tree/submodule entries are not
        auditable scope."""
        url = f"/repos/{owner}/{repo}/git/trees/{quote(tree_sha, safe='')}"
        if recursive:
            url = f"{url}?recursive=1"
        payload = await self._get(url)
        entries = payload.get("tree") if isinstance(payload, dict) else None
        paths = [
            str(e.get("path"))
            for e in (entries or [])
            if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")
        ]
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        return {"paths": paths, "truncated": truncated}

    async def get_branch_head_sha(self, owner: str, repo: str, branch: str) -> str:
        """Head commit sha of a branch (GET /repos/{o}/{r}/commits/{branch})."""
        body = await self._get(f"/repos/{owner}/{repo}/commits/{branch}")
        return str(body.get("sha") or "") if isinstance(body, dict) else ""

    async def compare_commits(
        self, owner: str, repo: str, base: str, head: str
    ) -> dict[str, Any]:
        """{"paths": [changed file paths], "truncated": bool} between two commits.

        The freshness path cannot trust a push payload's `commits` array: GitHub
        caps it at 20 commits and merge commits carry empty file lists, so
        merging a large PR silently under-reports what changed. The compare
        range is the honest answer.

        Renames are reported under BOTH names — the old path vanishing is
        exactly what makes an area's scope dead, so a rename must mark both the
        source and destination areas stale.
        """
        payload = await self._get(
            f"/repos/{owner}/{repo}/compare/"
            f"{quote(base, safe='')}...{quote(head, safe='')}"
        )
        files = payload.get("files") if isinstance(payload, dict) else None
        paths: list[str] = []
        for f in files or []:
            if not isinstance(f, dict):
                continue
            for key in ("filename", "previous_filename"):
                value = f.get(key)
                if value:
                    paths.append(str(value))
        # GitHub does not flag the 300-file cut itself, so infer it: a compare
        # that lands exactly on the cap is presumed clipped.
        truncated = len(files or []) >= MAX_COMPARE_FILES
        return {"paths": list(dict.fromkeys(paths)), "truncated": truncated}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any] | None:
        """GET /repos/{o}/{r}/branches/{branch} — None when the branch doesn't
        exist (404-safe), so callers can adopt-or-create idempotently."""
        try:
            return await self._get(f"/repos/{owner}/{repo}/branches/{branch}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    # ── Writes ───────────────────────────────────────────────────────────

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
    ) -> dict[str, Any]:
        return await self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )

    async def mark_pull_request_ready(self, owner: str, repo: str, number: int) -> None:
        """Flip a draft PR to ready-for-review. Draft state is GraphQL-only
        on GitHub (REST cannot un-draft), so: REST fetch for the node_id,
        then the markPullRequestReadyForReview mutation. No-op when the PR
        is already ready."""
        pr = await self.get_pull_request(owner, repo, number)
        if not pr.get("draft"):
            return
        node_id = str(pr.get("node_id") or "")
        if not node_id:
            raise RuntimeError(f"PR {owner}/{repo}#{number} payload has no node_id")
        mutation = (
            "mutation($id: ID!) {"
            " markPullRequestReadyForReview(input: {pullRequestId: $id})"
            " { pullRequest { isDraft } } }"
        )
        data = await self._post("/graphql", json={"query": mutation, "variables": {"id": node_id}})
        errors = data.get("errors") or []
        if errors:
            raise RuntimeError(
                f"markPullRequestReadyForReview failed for {owner}/{repo}#{number}: "
                f"{errors[0].get('message', 'unknown GraphQL error')}"
            )

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": state, "context": context, "description": description}
        if target_url:
            payload["target_url"] = target_url
        return await self._post(f"/repos/{owner}/{repo}/statuses/{sha}", json=payload)

    async def create_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """Post a plain conversation comment on a PR (PRs are issues here)."""
        return await self._post(
            f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )

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
        """Publish a PR review: a summary body plus optional inline comments.

        `comments` entries are GitHub's shape — {"path", "line", "side",
        "body"}. GitHub rejects the WHOLE review with 422 if any comment
        anchors to a line outside the diff, so callers must filter to the
        PR's changed lines; `review_publisher` also retries body-only on 422
        so a bad anchor degrades to a summary rather than losing the review.

        `event` stays COMMENT by default on purpose: APPROVE and
        REQUEST_CHANGES make the app a real reviewer, which interacts with
        branch protection and required-review counts. Whether OpenSweep should
        hold that power is a repo-owner decision, not a default.
        """
        payload: dict[str, Any] = {"body": body, "event": event}
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = comments
        return await self._post(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload
        )

    # ── Capability probes ────────────────────────────────────────────────

    async def check_write_access(self, owner: str, repo: str) -> bool | None:
        """Can this credential actually push to the repo?

        Deliberately NOT `GET /repos`.`permissions.push`: that field reports the
        authenticated *user's* role on the repo, not what the credential is
        granted. A read-only fine-grained PAT held by a repo admin reports
        `push: true` and then 403s at receive-pack — the exact trap this probe
        exists to avoid.

        So it asks the grant directly, by requesting a ref at the null SHA.
        GitHub authorizes first and validates the payload second, and no object
        with that SHA can exist, so a credential that may write is refused at
        validation while one that may not is refused at authorization. Nothing
        is ever created.

        Returns True (probe says writable), False (definitively denied), or
        None (inconclusive — no credential, transport error, or a status we
        don't recognize). ONLY False is a real answer: callers must treat None
        as "proceed", so a GitHub hiccup can never block legitimate work. That
        also keeps the True mapping non-load-bearing — if GitHub ever answers an
        authorized probe with something other than 422, this degrades to the
        old behavior instead of blocking a run that would have succeeded.
        """
        if not self.is_active:
            return None
        try:
            r = await self._client.post(
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": "refs/heads/opensweep-write-preflight", "sha": "0" * 40},
                headers=await self._request_headers(),
            )
        except Exception as exc:  # transport/timeout — never block on this
            logger.warning(
                f"write-access probe for {owner}/{repo} failed to run: {exc}",
                extra={"tag": "github"},
            )
            return None
        if r.status_code == 403:
            return False
        if r.status_code == 422:
            return True
        logger.info(
            f"write-access probe for {owner}/{repo} inconclusive ({r.status_code})",
            extra={"tag": "github"},
        )
        return None

    # ── Internals ────────────────────────────────────────────────────────

    async def _get(self, path: str) -> Any:
        if not self.is_active:
            raise RuntimeError("GitHubClient is not active (GITHUB_TOKEN unset)")
        r = await self._client.get(path, headers=await self._request_headers())
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, json: dict) -> dict[str, Any]:
        if not self.is_active:
            raise RuntimeError("GitHubClient is not active (GITHUB_TOKEN unset)")
        r = await self._client.post(path, json=json, headers=await self._request_headers())
        if r.status_code >= 400:
            logger.warning(f"GitHub POST {path} → {r.status_code} {r.text[:200]}", extra={"tag": "github"})
        r.raise_for_status()
        return r.json()


# One client per event loop, keyed by id(loop). httpx.AsyncClient binds its
# transport to the loop it first runs on; Celery tasks execute each invocation
# in a fresh asyncio.run() loop, so a process-wide singleton would resurface a
# client bound to a dead loop ("Event loop is closed"). The value keeps a
# strong reference to the loop so an id() is never reused while cached.
_clients_by_loop: dict[int, tuple[Any, GitHubClient]] = {}


def get_default_client() -> GitHubClient:
    """Loop-aware client helper; respects the current settings.GITHUB_TOKEN."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync/bootstrap context) — a fresh client is cheap
        # and will bind itself to whichever loop first uses it.
        return GitHubClient()

    entry = _clients_by_loop.get(id(loop))
    if entry is not None and entry[0] is loop:
        return entry[1]

    # Evict entries for closed loops so the cache stays tiny.
    for key in [k for k, (cached_loop, _) in _clients_by_loop.items() if cached_loop.is_closed()]:
        _clients_by_loop.pop(key, None)

    client = GitHubClient()
    _clients_by_loop[id(loop)] = (loop, client)
    return client


# Installation-token clients — same loop-aware caching, additionally keyed by
# installation id (each installation resolves its own short-lived tokens).
_installation_clients: dict[tuple[int, int], tuple[Any, GitHubClient]] = {}


def get_installation_client(installation_id: int) -> GitHubClient:
    """Loop-aware client whose requests carry an App installation token.

    Prefer `infrastructure.github_app.get_client_for_repo(repo)` — it applies
    the full precedence (installation token when the App covers the repo,
    else the PAT default client)."""
    from infrastructure.github_app import InstallationTokenSource

    installation_id = int(installation_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return GitHubClient(token_source=InstallationTokenSource(installation_id))

    key = (id(loop), installation_id)
    entry = _installation_clients.get(key)
    if entry is not None and entry[0] is loop:
        return entry[1]

    for k in [k for k, (cached_loop, _) in _installation_clients.items() if cached_loop.is_closed()]:
        _installation_clients.pop(k, None)

    client = GitHubClient(token_source=InstallationTokenSource(installation_id))
    _installation_clients[key] = (loop, client)
    return client


# PAT-connection clients — same loop-aware caching, keyed by connection uid.
# The token itself resolves per request (ConnectionTokenSource), so cached
# clients survive token rotation/removal.
_connection_clients: dict[tuple[int, str], tuple[Any, GitHubClient]] = {}


def get_connection_client(connection_uid: str) -> GitHubClient:
    """Loop-aware client whose requests carry a GitConnection(kind="pat") token.

    Prefer `infrastructure.github_app.get_client_for_repo(repo)` — it applies
    the full precedence (installation token → connection PAT → env PAT)."""
    from infrastructure.github_app import ConnectionTokenSource

    connection_uid = str(connection_uid)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return GitHubClient(token_source=ConnectionTokenSource(connection_uid))

    key = (id(loop), connection_uid)
    entry = _connection_clients.get(key)
    if entry is not None and entry[0] is loop:
        return entry[1]

    for k in [k for k, (cached_loop, _) in _connection_clients.items() if cached_loop.is_closed()]:
        _connection_clients.pop(k, None)

    client = GitHubClient(token_source=ConnectionTokenSource(connection_uid))
    _connection_clients[key] = (loop, client)
    return client
