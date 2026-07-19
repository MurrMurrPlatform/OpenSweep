"""PullRequest sync + convergence recompute (PLATFORM_V2_DESIGN.md §5, §7).

Head-driven: every trigger (webhook event, manual resync, verdict submission,
resolution transition) re-reads the PR head state and recomputes the predicate
from current data. Event order never accumulates state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from config import settings
from domains.delivery.models import PullRequest, Verdict, pr_key
from domains.delivery.schemas import (
    ConvergenceState,
    PullRequestDTO,
    SubmitVerdictRequest,
    VerdictDTO,
)
from domains.delivery.services import write_gate
from domains.delivery.services.convergence import compute_convergence, status_description
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.findings.models import Finding
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from infrastructure.git_providers import get_provider_client
from logging_config import logger


def pull_request_to_dto(pr: PullRequest) -> PullRequestDTO:
    convergence = None
    if pr.convergence:
        try:
            convergence = ConvergenceState(**pr.convergence)
        except Exception:
            convergence = None
    return PullRequestDTO(
        uid=pr.uid,
        repository_uid=pr.repository_uid,
        github_number=int(pr.github_number),
        title=pr.title or "",
        author=pr.author or "",
        url=pr.url or "",
        state=pr.state or "open",
        draft=bool(pr.draft),
        head_sha=pr.head_sha or "",
        head_ref=pr.head_ref or "",
        base_ref=pr.base_ref or "",
        base_is_default=bool(pr.base_is_default),
        ticket_uid=pr.ticket_uid or "",
        ci_state=pr.ci_state or "empty",
        ci_checks=list(pr.ci_checks or []),
        fix_rounds=int(pr.fix_rounds or 0),
        fix_rounds_exhausted=bool(pr.fix_rounds_exhausted),
        converged=bool(pr.converged),
        convergence=convergence,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        last_synced_at=pr.last_synced_at,
    )


def verdict_to_dto(v: Verdict) -> VerdictDTO:
    return VerdictDTO(
        uid=v.uid,
        pull_request_uid=v.pull_request_uid,
        repository_uid=v.repository_uid,
        sha=v.sha,
        result=v.result,
        new_blocking_findings=int(v.new_blocking_findings or 0),
        finding_uids=list(v.finding_uids or []),
        ac_results=list(v.ac_results or []),
        source_run_uid=v.source_run_uid or "",
        executor=v.executor or "manual",
        verification_status=v.verification_status or "",
        verification_run_uid=v.verification_run_uid or "",
        created_at=v.created_at,
    )


def review_status_for(
    result: str,
    new_blocking: int,
    verification_status: str = "",
    *,
    finding_titles: list[str] | None = None,
    depth: str = "",
) -> tuple[str, str]:
    """Map a review outcome to the `opensweep/review` commit-status pair — pure.

    "" result = review dispatched, no verdict yet."""
    if not result:
        suffix = f" (depth={depth})" if depth else ""
        return "pending", f"review in progress{suffix}"
    if result == "approve":
        return "success", "approved — 0 new blocking findings"
    if result == "needs_human":
        return "error", "review needs a human"
    # request_changes
    if verification_status == "pending":
        return "pending", f"{new_blocking} blocking finding(s) — verification in progress"
    titles = ", ".join(finding_titles or [])
    detail = f": {titles}" if titles else ""
    return "failure", f"{new_blocking} blocking finding(s){detail}"


async def publish_review_status(
    repo: Repository, pr: PullRequest, *, state: str, description: str
) -> None:
    """Post the `opensweep/review` commit status at head — best-effort."""
    if pr.state != "open" or not pr.head_sha:
        return
    client = get_provider_client(repo)
    if not client.is_active or not (repo.github_owner and repo.github_repo):
        return
    try:
        await client.create_commit_status(
            repo.github_owner,
            repo.github_repo,
            pr.head_sha,
            state=state,
            context=settings.OPENSWEEP_REVIEW_STATUS_CONTEXT,
            description=description[:140],
            target_url=f"{settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL}/pull-requests/{pr.uid}",
        )
    except Exception as exc:
        logger.warning(f"review status publish failed for {pr.pr_key}: {exc}", extra={"tag": "delivery"})


def pick_latest_verdict(verdicts: list, head_sha: str = "") -> Verdict | None:
    """Select the verdict the predicate should judge — pure, unit-testable.

    Verdicts whose sha matches the PR's current head are preferred over any
    other, regardless of creation time: a late-FINISHING review of an OLD
    commit must not displace a fresh approve of the current head (its verdict
    is stale by definition). Among the preferred pool (or all verdicts when
    none match head), latest (created_at, uid) wins — uid breaks ties
    deterministically for same-timestamp verdicts.
    """
    if not verdicts:
        return None
    at_head = [v for v in verdicts if head_sha and v.sha == head_sha]
    pool = at_head or list(verdicts)
    pool.sort(
        key=lambda v: (v.created_at or datetime.min.replace(tzinfo=UTC), v.uid or ""),
        reverse=True,
    )
    return pool[0]


async def latest_verdict_for(pr_uid: str, head_sha: str = "") -> Verdict | None:
    nodes = await Verdict.nodes.filter(pull_request_uid=pr_uid)
    return pick_latest_verdict(list(nodes), head_sha=head_sha)


# Mirrors the per-file patch budget of run changes (run_changes.PATCH_MAX_CHARS)
# — GitHub already omits `patch` for huge diffs, this is a defensive backstop.
PR_PATCH_MAX_CHARS = 200_000

# GitHub file statuses → the run-changes vocabulary the diff panel renders.
_PR_FILE_STATUS = {
    "added": "added",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
}


def github_files_to_changes(pr: PullRequest, payloads: list[dict]) -> dict:
    """Map `GET /pulls/{n}/files` payloads to the `/runs/{uid}/changes` shape.

    GitHub omits `patch` for binary files and oversized diffs; additions or
    deletions being counted tells the two apart (binary numstats are zero)."""
    files = []
    for f in payloads:
        path = str(f.get("filename") or "")
        if not path:
            continue
        status = _PR_FILE_STATUS.get(str(f.get("status") or ""), "modified")
        patch = str(f.get("patch") or "")
        additions = int(f.get("additions") or 0)
        deletions = int(f.get("deletions") or 0)
        too_large = False
        binary = False
        if not patch:
            if additions or deletions:
                too_large = True
            elif status != "renamed":  # a pure rename legitimately has no patch
                binary = True
        elif len(patch) > PR_PATCH_MAX_CHARS:
            patch = ""
            too_large = True
        files.append(
            {
                "path": path,
                "old_path": str(f.get("previous_filename") or ""),
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "patch": patch,
                "binary": binary,
                "too_large": too_large,
            }
        )
    files.sort(key=lambda f: f["path"])
    return {
        "source": "live",
        "base": pr.base_ref or "",
        "captured_at": None,
        "files": files,
        "tree": [],
    }


class PullRequestService:
    async def get_node(self, uid: str) -> PullRequest:
        pr = await PullRequest.nodes.get_or_none(uid=uid)
        if pr is None:
            raise HTTPException(status_code=404, detail=f"PullRequest {uid} not found")
        return pr

    async def list(
        self, *, repository_uid: str | None = None, state: str | None = None
    ) -> list[PullRequestDTO]:
        nodes = await PullRequest.nodes.all()
        out = [
            pull_request_to_dto(pr)
            for pr in nodes
            if (not repository_uid or pr.repository_uid == repository_uid)
            and (not state or pr.state == state)
        ]
        out.sort(
            key=lambda d: d.updated_at or d.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return out

    # ── Sync ─────────────────────────────────────────────────────────────

    async def _repo_and_client(self, repository_uid: str) -> tuple[Repository, object]:
        repo = await Repository.nodes.get_or_none(uid=repository_uid)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"Repository {repository_uid} not found")
        if not (repo.github_owner and repo.github_repo):
            raise HTTPException(status_code=400, detail="repository has no GitHub coordinates")
        client = get_provider_client(repo)
        if not client.is_active:
            raise HTTPException(status_code=503, detail="GitHub client inactive (GITHUB_TOKEN unset)")
        return repo, client

    async def apply_github_payload(self, repo: Repository, client, payload: dict) -> PullRequestDTO:
        """Upsert one GitHub `pull_request` payload, refresh checks, recompute."""
        pr = await self.upsert_from_payload(repo, payload)
        checks = []
        if pr.head_sha and pr.state == "open":
            try:
                raw = await client.list_check_runs(repo.github_owner, repo.github_repo, ref=pr.head_sha)
                checks = [
                    {
                        "name": c.get("name") or "",
                        "status": c.get("status") or "",
                        "conclusion": c.get("conclusion"),
                        "url": c.get("html_url") or "",
                    }
                    for c in raw
                ]
            except Exception as exc:
                logger.warning(
                    f"check-run fetch failed for PR #{pr.github_number}: {exc}",
                    extra={"tag": "delivery"},
                )
                checks = list(pr.ci_checks or [])
        pr.ci_checks = checks
        await pr.save()

        state = await self.recompute(pr)
        await self._publish_status(repo, pr, state)
        return pull_request_to_dto(pr)

    async def sync_from_github(self, repository_uid: str, github_number: int) -> PullRequestDTO:
        """Fetch PR + check runs at head from GitHub, upsert, recompute, publish."""
        repo, client = await self._repo_and_client(repository_uid)
        payload = await client.get_pull_request(repo.github_owner, repo.github_repo, github_number)
        return await self.apply_github_payload(repo, client, payload)

    async def files(self, pr: PullRequest) -> dict:
        """The PR's changed files with per-file unified patches, in the same
        shape as `/runs/{uid}/changes` so the frontend diff panel is shared."""
        repo, client = await self._repo_and_client(pr.repository_uid)
        payloads = await client.list_pull_request_files(
            repo.github_owner, repo.github_repo, int(pr.github_number)
        )
        return github_files_to_changes(pr, payloads)

    async def sync_repository(self, repository_uid: str) -> dict:
        """Full 2-way reconcile with GitHub — the queue's source of truth.

        Pulls every open PR from GitHub (so PRs opened outside OpenSweep appear in
        the queue) and re-syncs local PRs GitHub no longer lists as open (so
        externally merged/closed PRs leave it). Webhooks remain the realtime
        path; this is the safety net for missed or unconfigured deliveries."""
        repo, client = await self._repo_and_client(repository_uid)

        payloads = await client.list_pull_requests(
            repo.github_owner, repo.github_repo, state="open"
        )
        open_numbers: set[int] = set()
        synced = 0
        for payload in payloads:
            open_numbers.add(int(payload["number"]))
            await self.apply_github_payload(repo, client, payload)
            synced += 1

        # Locally-open PRs GitHub no longer reports open were merged/closed
        # (or force-deleted) outside OpenSweep — re-read them individually.
        stale = [
            pr
            for pr in await PullRequest.nodes.filter(repository_uid=repository_uid, state="open")
            if int(pr.github_number) not in open_numbers
        ]
        closed = 0
        for pr in stale:
            try:
                payload = await client.get_pull_request(
                    repo.github_owner, repo.github_repo, int(pr.github_number)
                )
                await self.apply_github_payload(repo, client, payload)
                closed += 1
            except Exception as exc:  # noqa: BLE001 — one dead PR must not stop the sweep
                logger.warning(
                    f"stale-PR resync failed for #{pr.github_number}: {exc}",
                    extra={"tag": "delivery"},
                )

        repo.last_synced_at = datetime.now(UTC)
        await repo.save()
        return {"repository_uid": repository_uid, "synced": synced, "closed": closed}

    async def upsert_from_payload(self, repo: Repository, payload: dict) -> PullRequest:
        """Idempotent upsert from a GitHub API / webhook `pull_request` object."""
        number = int(payload["number"])
        key = pr_key(repo.uid, number)
        pr = await PullRequest.nodes.get_or_none(pr_key=key)
        if pr is None:
            pr = PullRequest(
                uid=uuid4().hex, repository_uid=repo.uid, github_number=number, pr_key=key
            )

        head = payload.get("head") or {}
        base = payload.get("base") or {}
        previous_head = pr.head_sha or ""

        pr.title = payload.get("title") or ""
        pr.author = ((payload.get("user") or {}).get("login")) or ""
        pr.url = payload.get("html_url") or ""
        pr.draft = bool(payload.get("draft"))
        pr.state = "merged" if payload.get("merged") or payload.get("merged_at") else (
            payload.get("state") or "open"
        )
        pr.head_sha = head.get("sha") or ""
        pr.head_ref = head.get("ref") or ""
        pr.base_ref = base.get("ref") or ""
        pr.base_is_default = (base.get("ref") or "") == (repo.default_branch or "main")
        pr.last_synced_at = datetime.now(UTC)
        pr.updated_at = datetime.now(UTC)

        if previous_head and pr.head_sha and previous_head != pr.head_sha:
            # New push: stored CI checks belong to the old head. Drop them so
            # the predicate can't read stale green (§5.1). Verdict staleness is
            # handled by the predicate itself (verdict.sha != head).
            pr.ci_checks = []
            await write_audit(
                kind="pull_request.head_moved",
                subject_uid=pr.uid,
                subject_type="PullRequest",
                payload={"from": previous_head, "to": pr.head_sha},
            )
        await pr.save()
        return pr

    # ── Convergence ──────────────────────────────────────────────────────

    async def recompute(self, pr: PullRequest) -> ConvergenceState:
        # Local import: resolution_service imports models only; this avoids a
        # service-level cycle while reusing ensure_merge_policy.
        from domains.delivery.models import FindingResolution

        policy = await ensure_merge_policy(pr.repository_uid)
        resolutions = await FindingResolution.nodes.filter(pull_request_uid=pr.uid)
        items = []
        for r in resolutions:
            finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
            items.append(
                {
                    "state": r.state or "open",
                    "severity": (finding.severity if finding else "medium") or "medium",
                    "tags": list(finding.tags or []) if finding else [],
                    "override": r.blocking_override or "",
                }
            )

        # Prefer verdicts at the current head (finding: a late-finishing
        # review of an old sha must not displace a fresh approve).
        verdict = await latest_verdict_for(pr.uid, head_sha=pr.head_sha or "")
        latest = (
            {
                "sha": verdict.sha,
                "result": verdict.result,
                "new_blocking_findings": int(verdict.new_blocking_findings or 0),
            }
            if verdict
            else None
        )

        state = compute_convergence(
            head_sha=pr.head_sha or "",
            ci_checks=list(pr.ci_checks or []),
            latest_verdict=latest,
            resolutions=items,
            blocking_policy=dict(policy.blocking or {}),
            require_clean_round=bool(policy.require_clean_round),
            pr_state=pr.state or "open",
            draft=bool(pr.draft),
            base_is_default=bool(pr.base_is_default),
        )
        pr.converged = state.converged
        pr.convergence = state.model_dump(mode="json")
        # Keep the denormalized rollup field in sync — queue cards read
        # pr.ci_state directly, not the nested convergence snapshot.
        pr.ci_state = state.ci_state.value
        # Denormalize fix-round exhaustion while the policy is in hand so the
        # DTO can expose it without a second policy read.
        pr.fix_rounds_exhausted = write_gate.fix_rounds_exhausted(
            int(pr.fix_rounds or 0), int(policy.max_fix_rounds or 0)
        )
        pr.updated_at = datetime.now(UTC)
        await pr.save()
        return state

    async def recompute_and_publish(self, pr: PullRequest) -> ConvergenceState:
        state = await self.recompute(pr)
        repo = await Repository.nodes.get_or_none(uid=pr.repository_uid)
        if repo is not None:
            await self._publish_status(repo, pr, state)
        return state

    async def _publish_status(self, repo: Repository, pr: PullRequest, state: ConvergenceState) -> None:
        """Post the single `opensweep/converged` commit status at head (§5)."""
        if pr.state != "open" or not pr.head_sha:
            return
        client = get_provider_client(repo)
        if not client.is_active or not (repo.github_owner and repo.github_repo):
            return
        if state.converged:
            gh_state = "success"
        elif state.counts.blocking or state.ci_state == "red":
            gh_state = "failure"
        else:
            gh_state = "pending"
        try:
            await client.create_commit_status(
                repo.github_owner,
                repo.github_repo,
                pr.head_sha,
                state=gh_state,
                context=settings.OPENSWEEP_CONVERGED_STATUS_CONTEXT,
                description=status_description(state)[:140],
                target_url=f"{settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL}/pull-requests/{pr.uid}",
            )
        except Exception as exc:
            logger.warning(f"commit status publish failed for {pr.pr_key}: {exc}", extra={"tag": "delivery"})

    # ── Verdicts ─────────────────────────────────────────────────────────

    async def submit_verdict(
        self, pr_uid: str, req: SubmitVerdictRequest, *, actor_uid: str | None = None
    ) -> VerdictDTO:
        pr = await self.get_node(pr_uid)
        v = Verdict(
            uid=uuid4().hex,
            pull_request_uid=pr.uid,
            repository_uid=pr.repository_uid,
            sha=req.sha,
            result=req.result.value,
            new_blocking_findings=req.new_blocking_findings,
            finding_uids=req.finding_uids,
            ac_results=[a.model_dump() for a in req.ac_results],
            source_run_uid=req.source_run_uid,
            executor=req.executor,
        )
        # Skeptic pass (§A): a blocking review verdict is provisional while a
        # verification run challenges its findings. Adjusted verdicts
        # (executor="verification") are never re-verified.
        if (
            req.result.value == "request_changes"
            and req.new_blocking_findings > 0
            and req.finding_uids
            and req.executor != "verification"
        ):
            from domains.repositories.services.workflow import stage_auto

            if await stage_auto(pr.repository_uid, "verify"):
                v.verification_status = "pending"
        await v.save()
        await write_audit(
            kind="verdict.submitted",
            subject_uid=v.uid,
            subject_type="Verdict",
            actor_uid=actor_uid,
            payload={"pr": pr.pr_key, "sha": req.sha, "result": req.result.value},
        )
        await self.recompute_and_publish(pr)

        # Thread follow-through: the verdict lands on the PR's thread timeline.
        from domains.threads.services.hooks import note_verdict_for_pr

        await note_verdict_for_pr(pr.uid, result=v.result, verdict_uid=v.uid, sha=v.sha)

        repo = await Repository.nodes.get_or_none(uid=pr.repository_uid)
        if repo is not None:
            gh_state, description = review_status_for(
                v.result,
                int(v.new_blocking_findings or 0),
                v.verification_status or "",
            )
            await publish_review_status(repo, pr, state=gh_state, description=description)
        return verdict_to_dto(v)


# ── Ledger-mutation follow-through ───────────────────────────────────────────
# Convergence inputs live outside the PR node too: the repo MergePolicy and
# each Finding's status/severity. When those mutate, every affected OPEN PR
# must be recomputed + republished or its stored predicate goes stale.
# Both helpers are best-effort: one bad PR never blocks the mutation.


async def recompute_open_prs_for_repository(repository_uid: str) -> int:
    """Recompute + republish all OPEN PRs of a repository (policy changed)."""
    service = PullRequestService()
    count = 0
    nodes = await PullRequest.nodes.filter(repository_uid=repository_uid, state="open")
    for pr in nodes:
        try:
            await service.recompute_and_publish(pr)
            count += 1
        except Exception as exc:  # noqa: BLE001 — best-effort loop
            logger.warning(
                f"post-policy recompute failed for {pr.pr_key}: {exc}",
                extra={"tag": "delivery"},
            )
    return count


async def recompute_open_prs_for_finding(finding_uid: str) -> int:
    """Recompute + republish OPEN PRs holding a resolution for this finding
    (its status/severity changed underneath the ledger)."""
    from domains.delivery.models import FindingResolution

    service = PullRequestService()
    count = 0
    resolutions = await FindingResolution.nodes.filter(finding_uid=finding_uid)
    seen: set[str] = set()
    for r in resolutions:
        if r.pull_request_uid in seen:
            continue
        seen.add(r.pull_request_uid)
        try:
            pr = await PullRequest.nodes.get_or_none(uid=r.pull_request_uid)
            if pr is None or (pr.state or "open") != "open":
                continue
            await service.recompute_and_publish(pr)
            count += 1
        except Exception as exc:  # noqa: BLE001 — best-effort loop
            logger.warning(
                f"post-finding recompute failed for PR {r.pull_request_uid}: {exc}",
                extra={"tag": "delivery"},
            )
    return count
