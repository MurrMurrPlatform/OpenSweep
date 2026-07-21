"""Checked stamps: record on run completion, derive freshness at query time.

record_for_run replaces CoverageService.record_for_run — one stamp per scope
the run touched, no concern dimension. Scopes are Doc uids (or the
repository uid). Freshness is a per-doc view: last stamp vs
code_changed_at.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from domains.checked.models import Checked
from domains.findings.models import Finding
from domains.runs.models import Run
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from infrastructure.git_providers import get_provider_client


# V3 runs land in awaiting_input after a successful turn (the conversation
# stays open); "completed"/"ended" cover legacy + explicitly closed runs.
_SUCCESS_STATUSES = {"awaiting_input", "completed", "ended"}


def _outcome_for_run(*, status: str, findings_count: int) -> str:
    if status in _SUCCESS_STATUSES:
        return "findings" if findings_count else "clean"
    return "failed"


def _coverage_fields(
    *, usage: dict[str, Any], target: dict[str, Any]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """(covered_paths, skipped_paths, lens_verdicts) for a run's stamps.

    Agent-reported coverage (usage["coverage"], written by complete_run) wins;
    when the agent didn't report covered_paths we fall back to the dispatched
    target paths — the run was pointed there, so that is the best available
    claim of what it looked at. Pure for testability."""
    coverage = dict(usage.get("coverage") or {})

    def _paths(value: Any) -> list[str]:
        # Shape guard: a stray string here would iterate per character.
        return [str(p) for p in (value if isinstance(value, (list, tuple)) else []) if p]

    covered = _paths(coverage.get("covered_paths"))
    if not covered:
        covered = _paths(target.get("paths"))
    skipped = _paths(coverage.get("skipped_paths"))
    verdicts = [v for v in (coverage.get("lens_verdicts") or []) if isinstance(v, dict)]
    return covered, skipped, verdicts


async def record_for_run(*, run_uid: str) -> list[Checked]:
    """Stamp every scope the run touched: the run's target docs plus any
    docs whose watch_paths its findings landed on; the repository itself
    when no doc was involved."""
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return []
    repo = await Repository.nodes.get_or_none(uid=run.repository_uid)

    findings = [f for f in await Finding.nodes.all() if (f.source_run_uid or "") == run.uid]

    scopes: list[str] = []
    target = dict(run.target or {})
    raw = target.get("doc_uids") or target.get("doc_uid") or []
    if isinstance(raw, str):
        raw = [raw]
    for uid in raw:
        if uid and uid not in scopes:
            scopes.append(str(uid))
    affected: list[str] = []
    for f in findings:
        affected.extend(str(p) for p in (f.affected_paths or []) if p)
    if affected:
        from domains.docs.models import Doc
        from domains.docs.services.doc_freshness import watches_path

        docs = [
            d for d in await Doc.nodes.all() if d.repository_uid == run.repository_uid
        ]
        for d in docs:
            if d.uid in scopes or not d.watch_paths:
                continue
            if any(watches_path(list(d.watch_paths), p) for p in affected):
                scopes.append(d.uid)
    if not scopes:
        scopes = [run.repository_uid]

    revision = await _repository_revision(repo, run=run)
    outcome = _outcome_for_run(status=run.status or "", findings_count=len(findings))
    now = run.completed_at or datetime.now(UTC)
    covered_paths, skipped_paths, lens_verdicts = _coverage_fields(
        usage=dict(run.usage or {}), target=target
    )

    stamps: list[Checked] = []
    for scope_uid in scopes:
        c = Checked(
            uid=uuid4().hex,
            repository_uid=run.repository_uid,
            scope_uid=scope_uid,
            run_uid=run.uid,
            revision=revision,
            outcome=outcome,
            checked_at=now,
            covered_paths=covered_paths,
            skipped_paths=skipped_paths,
            lens_verdicts=lens_verdicts,
        )
        await c.save()
        stamps.append(c)

    if stamps:
        await write_audit(
            kind="checked.recorded",
            subject_uid=run.uid,
            subject_type="Run",
            actor_uid=run.executor,
            payload={"scopes": len(stamps), "outcome": outcome, "revision": revision},
        )
    return stamps


async def stamps_for_paths(
    repository_uid: str, paths: list[str], *, limit: int = 10
) -> list[Checked]:
    """The repo's Checked stamps whose covered_paths overlap `paths`
    ("/"-boundary prefix semantics, either direction), newest first.

    Backs the area detail's coverage strip: an area's scope paths in, the
    last looks that touched them out."""
    from domains.docs.services.doc_freshness import watches_path

    wanted = [str(p) for p in paths if p]
    if not wanted:
        return []

    def _overlaps(c: Checked) -> bool:
        for covered in (str(p) for p in (c.covered_paths or []) if p):
            for p in wanted:
                if watches_path([p], covered) or watches_path([covered], p):
                    return True
        return False

    rows = [
        c
        for c in await Checked.nodes.all()
        if c.repository_uid == repository_uid and _overlaps(c)
    ]
    rows.sort(key=lambda c: _dt(c.checked_at), reverse=True)
    return rows[: max(limit, 0)]


async def freshness(*, repository_uid: str) -> list[dict[str, Any]]:
    """Per doc page (plus the repo-level scope): the latest stamp and whether
    the code moved past it. `never checked` scopes are included with
    last_checked=None so the UI can badge them."""
    from domains.docs.models import Doc

    latest: dict[str, Checked] = {}
    for c in await Checked.nodes.all():
        if c.repository_uid != repository_uid:
            continue
        old = latest.get(c.scope_uid)
        if old is None or _dt(c.checked_at) > _dt(old.checked_at):
            latest[c.scope_uid] = c

    out: list[dict[str, Any]] = []
    docs = [d for d in await Doc.nodes.all() if d.repository_uid == repository_uid]
    for d in docs:
        c = latest.get(d.uid)
        changed_since = bool(
            c is not None
            and d.code_changed_at
            and c.checked_at
            and d.code_changed_at > c.checked_at
        )
        out.append(
            {
                "scope_uid": d.uid,
                "last_checked": c.checked_at if c else None,
                "revision": (c.revision or "") if c else "",
                "outcome": (c.outcome or "") if c else "",
                "code_changed_since": changed_since,
            }
        )
    repo_stamp = latest.get(repository_uid)
    if repo_stamp is not None:
        out.append(
            {
                "scope_uid": repository_uid,
                "last_checked": repo_stamp.checked_at,
                "revision": repo_stamp.revision or "",
                "outcome": repo_stamp.outcome or "",
                "code_changed_since": False,
            }
        )
    return out


def _dt(value) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


async def _repository_revision(repo: Repository | None, *, run: Run) -> str:
    # Prefer the sha the run's workspace was cloned at when recorded.
    usage = dict(run.usage or {})
    for key in ("cloned_at_sha", "head_sha"):
        value = (usage.get("input") or {}).get(key) or usage.get(key)
        if value:
            return str(value)
    if repo is None:
        return ""
    metadata = dict(repo.metadata or {})
    for key in ("current_revision", "last_revision", "head_sha", "commit_sha"):
        if metadata.get(key):
            return str(metadata[key])
    client = get_provider_client(repo)
    if client.is_active and repo.github_owner and repo.github_repo:
        try:
            return await client.get_branch_head_sha(
                repo.github_owner, repo.github_repo, repo.default_branch or "main"
            )
        except Exception:
            return ""
    return ""
