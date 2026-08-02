"""Sandbox lifecycle — fresh `git clone` from GitHub based isolation."""

import asyncio
import re
import shutil
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException

from config import settings
from domains.execution.models import Sandbox
from domains.execution.schemas import SandboxDTO, SandboxStatus
from domains.repositories.schemas import RepositoryDTO
from infrastructure.audit import write_audit
from infrastructure.code_graph import index_code_graph
from infrastructure.git_safety import GitSafetyMode, safety_mode
from infrastructure.git_providers import get_git_credentials
from infrastructure.git_auth import git_auth_extraheader
from logging_config import logger


#: A git ref must start with an alphanumeric and use only branch-safe chars.
#: This blocks a name that would parse as a git option (leading '-', e.g.
#: '--upload-pack=...') or escape a pathspec ('..') when it reaches
#: `git fetch`/`git checkout` argv.
_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _assert_safe_git_ref(name: str, *, field: str) -> None:
    n = (name or "").strip()
    if not _SAFE_GIT_REF.match(n) or ".." in n:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {name!r}")


def _to_user_host_path(container_path: str) -> str:
    """Translate /host/sandboxes/<uid>/ → ~/.opensweep/sandboxes/<uid>/ (display only)."""
    mount = settings.OPENSWEEP_SANDBOX_HOST_MOUNT
    if container_path.startswith(mount):
        return container_path.replace(mount, settings.OPENSWEEP_SANDBOX_HOST_PATH, 1)
    return container_path


class SandboxService:
    async def create_for_discovery(
        self,
        *,
        repository: RepositoryDTO,
        agent_run_uid: str,
        source_branch: str | None = None,
        extra_refs: list[str] | None = None,
    ) -> SandboxDTO:
        """Cheap throwaway clone for read-only discovery runs.

        Same clone plumbing as write workspaces. Used so coding-agent CLIs like opencode have
        a writable cwd where they can inspect/build/test, but those edits get
        destroyed shortly after the run finishes; the model's actual
        product is the JSON findings it emits to stdout, not the diff.

        `extra_refs` are additional branches fetched into origin/* — PR review
        runs pass the base ref so `git diff base...head` resolves without the
        clone having to fetch every branch.
        """
        return await self._create(
            repository=repository,
            source_branch=source_branch or repository.default_branch or "main",
            sandbox_branch=f"opensweep-discovery/{agent_run_uid[:12]}",
            extra_refs=extra_refs,
        )

    async def create_for_write(
        self,
        *,
        repository: RepositoryDTO,
        agent_run_uid: str,
        work_branch: str,
        base_branch: str | None = None,
        checkout_existing: bool = False,
    ) -> SandboxDTO:
        """Write sandbox for implement/fix runs (PLATFORM_V2_DESIGN.md §6).

        Same GitHub-clone plumbing as discovery (per-invocation extraHeader
        token, shallow clone + targeted branch fetches). The agent edits and
        commits INSIDE this clone; it never pushes — the platform validates
        the git state (write_gate) and pushes with its own credentials.

        checkout_existing=True (fix-runs / branch adoption): check out the
        existing remote branch `work_branch` — it must exist. Otherwise
        (implement-runs) `work_branch` is created from `base_branch`
        (default: the repository default branch).
        """
        return await self._create(
            repository=repository,
            source_branch=base_branch or repository.default_branch or "main",
            sandbox_branch=work_branch,
            purpose="write",
            checkout_existing=checkout_existing,
            agent_run_uid=agent_run_uid,
        )

    async def _create(
        self,
        *,
        repository: RepositoryDTO,
        source_branch: str,
        sandbox_branch: str,
        purpose: str = "discovery",
        checkout_existing: bool = False,
        agent_run_uid: str = "",
        extra_refs: list[str] | None = None,
    ) -> SandboxDTO:
        """Shared clone path for discovery and write workspaces.

        The sandbox is a fresh clone from GitHub — the platform is GitHub-only,
        so there is no local working copy to clone from. Retention is ONE
        sliding window for every purpose (V3 §7, default 7 days) — each turn
        pushes cleanup_after out via touch().
        """
        # Both refs reach git argv (fetch/checkout); reject option-injection.
        _assert_safe_git_ref(source_branch, field="source_branch")
        _assert_safe_git_ref(sandbox_branch, field="sandbox_branch")
        owner = (repository.github_owner or "").strip()
        repo_name = (repository.github_repo or "").strip()
        if not owner or not repo_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot sandbox repository '{repository.slug}': "
                    "github_owner/github_repo are not configured"
                ),
            )
        # Installation token when the App covers this repo, else the PAT
        # (infrastructure/git_providers.get_git_credentials) — passed explicitly
        # so redaction below matches whatever credential was actually used.
        git_token = await get_git_credentials(repository)
        if not git_token:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot sandbox: no GitHub credential — connect the GitHub "
                    "App (or set GITHUB_TOKEN); sandbox clones pull from GitHub "
                    "and require an authenticated token"
                ),
            )

        uid = uuid4().hex[:16]
        container_path = f"{settings.OPENSWEEP_SANDBOX_HOST_MOUNT.rstrip('/')}/{uid}"
        host_path = _to_user_host_path(container_path)
        retention = datetime.now(UTC) + timedelta(
            hours=int(settings.OPENSWEEP_WORKSPACE_RETENTION_HOURS)
        )

        sandbox = Sandbox(
            uid=uid,
            repository_uid=repository.uid,
            host_path=host_path,
            container_path=container_path,
            source_branch=source_branch,
            sandbox_branch=sandbox_branch,
            purpose=purpose,
            status=SandboxStatus.PREPARING.value,
            cleanup_after=retention,
        )
        await sandbox.save()

        # Auth via a transient `-c http.extraHeader=…` rather than a
        # token-in-URL clone: `git -c` config applies to this invocation only
        # and is NOT persisted into the sandbox's .git/config, whereas
        # `https://x:TOKEN@github.com/…` writes the credential into
        # remote.origin.url — leaking it to everything that later runs inside
        # the sandbox.
        clone_cmd = [
            "git",
            "-c",
            git_auth_extraheader(git_token),
            "clone",
        ]
        # Depth rationale: review-runs execute `git diff base...head`, which
        # needs enough history to find a merge-base; depth 200 covers typical
        # PRs. A shallow clone is single-branch (remote HEAD); the refs a run
        # actually needs (source branch, PR base, existing work branch) are
        # fetched individually below instead of pulling depth-N history for
        # EVERY branch — on branchy repos that was most of the dispatch time.
        # OPENSWEEP_SANDBOX_CLONE_DEPTH=0 is the escape hatch for pathological
        # cases: full clone, all branches, no targeted fetches needed.
        depth = int(settings.OPENSWEEP_SANDBOX_CLONE_DEPTH)
        if depth > 0:
            clone_cmd += ["--depth", str(depth)]
        else:
            clone_cmd += ["--no-single-branch"]
        clone_cmd += [f"https://github.com/{owner}/{repo_name}.git", container_path]

        # Write sandboxes commit as the agent identity so PR history is
        # attributable; discovery clones keep the local throwaway identity.
        git_user, git_email = (
            ("opensweep-agent[bot]", "agents@opensweep.dev")
            if purpose == "write"
            else ("OpenSweep", "opensweep@local.dev")
        )
        try:
            with safety_mode(GitSafetyMode.SANDBOX, allowed_paths=[container_path]):
                await _run(clone_cmd, redact_token=git_token)
                await _run(["git", "-C", container_path, "config", "user.email", git_email])
                await _run(["git", "-C", container_path, "config", "user.name", git_user])
                if depth > 0:
                    # Targeted fetches replace --no-single-branch. Auth is the
                    # same transient extraHeader as the clone — nothing inside
                    # the sandbox can fetch later, so every needed ref must be
                    # present NOW. Only the existing work branch (fix runs) is
                    # required; source/base fall back like the checkout below.
                    wanted: list[tuple[str, bool]] = [(source_branch, False)]
                    wanted += [(r, False) for r in (extra_refs or [])]
                    if checkout_existing:
                        wanted.append((sandbox_branch, True))
                    seen: set[str] = set()
                    for ref, required in wanted:
                        ref = (ref or "").strip()
                        if not ref or ref in seen:
                            continue
                        seen.add(ref)
                        await _fetch_branch(
                            container_path,
                            ref,
                            git_token=git_token,
                            depth=depth,
                            required=required,
                        )
                if checkout_existing:
                    # Fix-runs / branch adoption: the remote branch MUST exist
                    # (fetched as a required ref above). A failure here is
                    # a real error — never silently fork a new branch.
                    await _run(["git", "-C", container_path, "checkout", sandbox_branch])
                else:
                    # Best-effort checkout of the source branch (fetched
                    # above); if it doesn't exist, fall back to the clone's
                    # default HEAD.
                    try:
                        await _run(["git", "-C", container_path, "checkout", source_branch])
                    except RuntimeError:
                        pass
                    await _run(["git", "-C", container_path, "checkout", "-b", sandbox_branch])
        except Exception as exc:
            sandbox.status = SandboxStatus.FAILED.value
            sandbox.error = str(exc)[:500]
            await sandbox.save()
            logger.warning(f"Sandbox prep failed: {exc}", extra={"tag": "sandbox"})
            raise

        # KNOWLEDGE_V3_CODE_GRAPH.md §2: index the fresh clone so the agent's
        # first structural query is instant. Runs in the same background prep
        # pipeline as the clone (never inside an HTTP request) and covers
        # workspace RECREATION too — every path funnels through _create.
        # Best-effort: no binary / failed index just means no code-graph
        # tools this workspace.
        indexed = await index_code_graph(container_path)

        sandbox.status = SandboxStatus.READY.value
        await sandbox.save()
        await write_audit(
            kind="sandbox.created", subject_uid=sandbox.uid, subject_type="Sandbox",
            payload={
                "host_path": host_path,
                "purpose": purpose,
                "branch": sandbox_branch,
                "agent_run_uid": agent_run_uid,
                "code_graph_indexed": indexed,
            },
        )
        return _to_dto(sandbox)

    async def update_status(self, sandbox_uid: str, *, status: SandboxStatus, error: str = "") -> SandboxDTO:
        sb = await Sandbox.nodes.get_or_none(uid=sandbox_uid)
        if sb is None:
            raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_uid} not found")
        sb.status = status.value
        if error:
            sb.error = error[:500]
        await sb.save()
        return _to_dto(sb)

    async def touch(self, sandbox_uid: str) -> None:
        """Slide the retention window (V3 §7): cleanup_after = now + retention.
        Called on every turn so an active conversation's workspace never
        expires underneath it. Silent when the sandbox is gone."""
        sb = await Sandbox.nodes.get_or_none(uid=sandbox_uid)
        if sb is None or sb.status == SandboxStatus.DESTROYED.value:
            return
        sb.cleanup_after = datetime.now(UTC) + timedelta(
            hours=int(settings.OPENSWEEP_WORKSPACE_RETENTION_HOURS)
        )
        await sb.save()

    async def list_active(self) -> list[SandboxDTO]:
        sbs = await Sandbox.nodes.all()
        return [_to_dto(s) for s in sbs if s.status != SandboxStatus.DESTROYED.value]

    async def destroy(self, sandbox_uid: str, *, actor_uid: str | None = None) -> SandboxDTO:
        sb = await Sandbox.nodes.get_or_none(uid=sandbox_uid)
        if sb is None:
            raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_uid} not found")
        if sb.status == SandboxStatus.DESTROYED.value:
            return _to_dto(sb)
        try:
            shutil.rmtree(sb.container_path, ignore_errors=True)
        except Exception as exc:
            logger.warning(f"rmtree failed for {sb.container_path}: {exc}", extra={"tag": "sandbox"})
        sb.status = SandboxStatus.DESTROYED.value
        sb.destroyed_at = datetime.now(UTC)
        await sb.save()
        await write_audit(
            kind="sandbox.destroyed", subject_uid=sb.uid, subject_type="Sandbox",
            actor_uid=actor_uid,
        )
        return _to_dto(sb)

    async def cleanup_expired(self) -> int:
        """Destroy sandboxes whose cleanup_after has passed. Returns count destroyed.

        V3 §7: when a run's workspace expires, the run is told — sandbox_uid
        cleared, a system transcript event appended, and awaiting_input runs
        move to ended. The conversation stays followable: a follow-up message
        recreates the workspace from Run.workspace_spec.
        """
        now = datetime.now(UTC)
        sbs = await Sandbox.nodes.all()
        count = 0
        for sb in sbs:
            if sb.status == SandboxStatus.DESTROYED.value:
                continue
            if sb.cleanup_after and sb.cleanup_after <= now:
                await self.destroy(sb.uid)
                await self._notify_runs_workspace_expired(sb.uid, now=now)
                count += 1
        return count

    async def _notify_runs_workspace_expired(self, sandbox_uid: str, *, now: datetime) -> None:
        # Local import: investigations already imports the execution domain.
        from domains.runs.models import Run
        from domains.runs.schemas import RunStatus
        from domains.runs.services.run_events import append_event

        for run in await Run.nodes.filter(sandbox_uid=sandbox_uid):
            run.sandbox_uid = ""
            if run.status == RunStatus.AWAITING_INPUT.value:
                run.status = RunStatus.ENDED.value
                run.ended_at = now
            run.updated_at = now
            await run.save()
            append_event(
                run.uid,
                "system",
                kind="workspace_expired",
                text="workspace expired — a follow-up message will rebuild it",
            )


async def _fetch_branch(
    container_path: str,
    ref: str,
    *,
    git_token: str,
    depth: int,
    required: bool,
) -> None:
    """Shallow-fetch one branch into refs/remotes/origin/<ref>, skipping refs
    the clone already brought in. `required=False` refs (source/base branches)
    degrade to a warning — the checkout below falls back to the clone's HEAD,
    matching the old best-effort behavior."""
    try:
        await _run(
            [
                "git", "-C", container_path,
                "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}",
            ]
        )
        return  # already present (e.g. the remote HEAD the clone fetched)
    except RuntimeError:
        pass
    cmd = ["git", "-c", git_auth_extraheader(git_token), "-C", container_path, "fetch"]
    if depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += ["origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"]
    try:
        await _run(cmd, redact_token=git_token)
    except RuntimeError as exc:
        if required:
            raise
        logger.warning(
            f"sandbox fetch of optional ref {ref!r} failed: {exc}",
            extra={"tag": "sandbox"},
        )


async def _run(cmd: list[str], *, redact_token: str = "") -> None:
    """Run a shell command, raising on non-zero exit.

    The error message is redacted: the GitHub credential (PAT or installation
    token — whichever was passed) travels in the clone command's extraHeader
    and must never land in Sandbox.error or the logs.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        # Filter the extraHeader argv out of the message entirely (mirrors
        # write_gate._git) — the token replace below stays as a second layer.
        shown = " ".join(a for a in cmd if not a.startswith("http.extraHeader"))
        message = f"{shown} failed: {err.decode(errors='replace')[:300]}"
        for token in (redact_token, settings.GITHUB_TOKEN):
            if token:
                message = message.replace(token, "***")
        raise RuntimeError(message)


def _to_dto(sb: Sandbox) -> SandboxDTO:
    return SandboxDTO(
        uid=sb.uid,
        repository_uid=sb.repository_uid,
        host_path=sb.host_path,
        container_path=sb.container_path,
        source_branch=sb.source_branch or "main",
        sandbox_branch=sb.sandbox_branch or "opensweep/work",
        purpose=getattr(sb, "purpose", None) or "discovery",
        status=sb.status,
        created_at=sb.created_at,
        destroyed_at=sb.destroyed_at,
        cleanup_after=sb.cleanup_after,
        error=sb.error or "",
    )


def sandbox_to_dto(sb: Sandbox) -> SandboxDTO:
    return _to_dto(sb)
