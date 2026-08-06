"""Checked stamps: record audit coverage on run completion.

record_for_run replaces CoverageService.record_for_run — one stamp per scope
the run touched, no concern dimension. Scopes are Doc uids (or the
repository uid). Checked stamps are audit-COVERAGE history (when/at-what-
revision/with-what-outcome a scope was last looked at), NOT a freshness
signal: staleness is the single derived review axis on the Doc/Area
(code_changed_at > last_reviewed_at). `audit_coverage` exposes the per-scope
latest stamp; it deliberately does not derive any "changed since" flag.

Coverage is recorded on two axes, and keeping them apart is the point:

- `covered_paths` is the CLAIM — what the agent said it examined, or, when it
  said nothing, the scope it was dispatched at. Entries naming files the
  repository does not have are dropped (`validate_claim`).
- `covered_paths_observed` is the MEASUREMENT — the files the run was seen to
  open, derived from its own tool_use events (`observed_coverage`).

`coverage_source` says which of the two a stamp actually rests on, so a
reader can tell evidence from intent instead of having to trust the claim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from domains.checked.models import Checked
from domains.findings.models import Finding
from domains.repositories.services.path_matching import normalize_path
from domains.runs.models import Run
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from infrastructure.git_providers import get_provider_client
from logging_config import logger


# V3 runs land in awaiting_input after a successful turn (the conversation
# stays open); "completed"/"ended" cover legacy + explicitly closed runs.
_SUCCESS_STATUSES = {"awaiting_input", "completed", "ended"}


def _outcome_for_run(*, status: str, findings_count: int) -> str:
    if status in _SUCCESS_STATUSES:
        return "findings" if findings_count else "clean"
    return "failed"


def validate_claim(
    claimed: list[str], *, tree: list[str] | None
) -> tuple[list[str], list[str]]:
    """Split a coverage claim into (paths the repo has, paths it does not). Pure.

    A claim is only worth storing if it names something real. An entry is kept
    when it matches a tracked file exactly OR is a prefix of one — agents
    legitimately report a directory for a scope they swept. `tree=None` means
    the file list was unavailable (a degraded tree, a torn-down workspace), and
    then nothing is DROPPED: absence of the tree is not evidence against the
    claim.

    Normalizing is unconditional, though. `path_recency` keys coverage recency
    on the exact path string, so "src/" and "src" would otherwise be two
    different rotation memories for one directory.
    """
    paths = [p for p in (normalize_path(str(raw)) for raw in claimed) if p]
    if tree is None:
        return paths, []
    files = {normalize_path(p) for p in tree if p}
    kept: list[str] = []
    unknown: list[str] = []
    for path in paths:
        if path in files or any(f.startswith(path + "/") for f in files):
            kept.append(path)
        else:
            unknown.append(path)
    return kept, unknown


def _coverage_fields(
    *,
    usage: dict[str, Any],
    target: dict[str, Any],
    observed: list[str] | None = None,
    tree: list[str] | None = None,
) -> tuple[list[str], list[str], list[dict[str, Any]], str, list[str], list[str]]:
    """(covered, skipped, lens_verdicts, coverage_source, observed, unknown).

    Three sources, strongest first:

    - `observed` — paths derived from the run's own tool_use events
      (observed_coverage). Measurement, not assertion. Available for
      claude_code runs only; opencode emits no per-tool-call events.
    - agent-reported `usage["coverage"]["covered_paths"]` — a claim, kept when
      it survives `validate_claim`.
    - the dispatched `target["paths"]` — what the run was POINTED at. Retained
      because dropping it would blank the coverage strip for every run that
      omits the contract, but labelled `inferred` so no reader mistakes intent
      for evidence.

    `covered_paths` stays the claim in all cases, so existing readers keep
    working unchanged; `coverage_source` records which of the three we had and
    `covered_paths_observed` carries the measurement alongside. The gap between
    the two is what makes an over-broad claim visible instead of authoritative.

    The label is still deliberately NOT used to rank or filter rotation: it
    records what evidence a run left, and ranking on it would rank on prompt
    compliance and starve the areas whose agents DID report. See
    audit_selection.coverage_recency_for for the worked argument. Pure for
    testability."""
    coverage = dict(usage.get("coverage") or {})

    def _paths(value: Any) -> list[str]:
        # Shape guard: a stray string here would iterate per character.
        return [str(p) for p in (value if isinstance(value, (list, tuple)) else []) if p]

    observed = list(observed or [])
    source = "reported"
    covered = _paths(coverage.get("covered_paths"))
    if not covered:
        covered = _paths(target.get("paths"))
        source = "inferred"
    covered, unknown = validate_claim(covered, tree=tree)
    if observed:
        source = "observed"
    skipped, _ = validate_claim(_paths(coverage.get("skipped_paths")), tree=tree)
    verdicts = [v for v in (coverage.get("lens_verdicts") or []) if isinstance(v, dict)]
    return covered, skipped, verdicts, source, observed, unknown


async def record_for_run(*, run_uid: str) -> list[Checked]:
    """Stamp every scope the run touched: the run's target docs plus any
    docs whose watch_paths its findings landed on; the repository itself
    when no doc was involved."""
    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is None:
        return []
    repo = await Repository.nodes.get_or_none(uid=run.repository_uid)

    findings = list(
        await Finding.nodes.filter(
            repository_uid=run.repository_uid, source_run_uid=run.uid
        )
    )

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
        from domains.repositories.services.path_matching import watches_path

        docs = list(await Doc.nodes.filter(repository_uid=run.repository_uid))
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
    workspace_root = str(
        ((run.usage or {}).get("input") or {}).get("repository_local_path") or ""
    )
    observed = _observed_for(run.uid, workspace_root)
    tree = await _workspace_tree(workspace_root)
    (
        covered_paths,
        skipped_paths,
        lens_verdicts,
        coverage_source,
        observed_paths,
        unknown_paths,
    ) = _coverage_fields(
        usage=dict(run.usage or {}), target=target, observed=observed, tree=tree
    )
    if unknown_paths:
        # Not fatal — but a claim naming files the repository does not have is
        # exactly the signal this validation exists to surface, so it must not
        # vanish silently.
        logger.warning(
            f"run {run.uid}: dropped {len(unknown_paths)} claimed coverage "
            f"path(s) matching no file in the workspace: {unknown_paths[:5]}",
            extra={"tag": "checked"},
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
            covered_paths_observed=observed_paths,
            lens_verdicts=lens_verdicts,
            coverage_source=coverage_source,
        )
        await c.save()
        stamps.append(c)

    if stamps:
        await write_audit(
            kind="checked.recorded",
            subject_uid=run.uid,
            subject_type="Run",
            actor_uid=run.executor,
            payload={
                "scopes": len(stamps),
                "outcome": outcome,
                "revision": revision,
                "coverage_source": coverage_source,
                "claimed": len(covered_paths),
                "observed": len(observed_paths),
                "unverifiable": len(unknown_paths),
            },
        )
    return stamps


def _observed_for(run_uid: str, workspace_root: str) -> list[str]:
    """Observed paths, best-effort — evidence is a bonus, never turn-fatal."""
    if not workspace_root:
        return []
    try:
        from domains.checked.services.observed_coverage import observed_paths_for_run

        return observed_paths_for_run(run_uid, workspace_root)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"observed coverage unavailable for run {run_uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "checked"},
        )
        return []


async def _workspace_tree(workspace_root: str) -> list[str] | None:
    """The workspace's tracked files, or None when it can't be listed.

    None is meaningfully different from []: it means "could not check", and
    `validate_claim` then drops nothing. A torn-down workspace must never
    retroactively invalidate a run's honest report.

    Uses `git ls-files` directly rather than run_changes.compute_changes —
    that computes a full diff with patches, which is a lot of work to throw
    away when all we need is the file list.
    """
    if not workspace_root:
        return None
    try:
        from domains.runs.services.run_changes import _git

        out = await _git(workspace_root, "ls-files")
        return [line for line in out.splitlines() if line.strip()] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"workspace tree unavailable at {workspace_root!r}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "checked"},
        )
        return None


async def stamps_for_paths(
    repository_uid: str, paths: list[str], *, limit: int = 10
) -> list[Checked]:
    """The repo's Checked stamps whose covered_paths overlap `paths`
    ("/"-boundary prefix semantics, either direction), newest first.

    Backs the area detail's coverage strip: an area's scope paths in, the
    last looks that touched them out."""
    from domains.repositories.services.path_matching import watches_path

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
        for c in await Checked.nodes.filter(repository_uid=repository_uid)
        if _overlaps(c)
    ]
    rows.sort(key=lambda c: _dt(c.checked_at), reverse=True)
    return rows[: max(limit, 0)]


async def audit_coverage(*, repository_uid: str) -> list[dict[str, Any]]:
    """Per doc page (plus the repo-level scope): the latest audit-coverage
    stamp — when it was last checked, at what revision, with what outcome.

    This is audit-coverage HISTORY, not a freshness signal: staleness is the
    single derived review axis (Doc.stale, code_changed_at > last_reviewed_at)
    and lives on the Doc/Area DTO. A code-quality audit stamping coverage here
    never clears docs-stale. `never checked` scopes are included with
    last_checked=None so the UI can badge coverage gaps."""
    from domains.docs.models import Doc

    latest: dict[str, Checked] = {}
    for c in await Checked.nodes.filter(repository_uid=repository_uid):
        old = latest.get(c.scope_uid)
        if old is None or _dt(c.checked_at) > _dt(old.checked_at):
            latest[c.scope_uid] = c

    out: list[dict[str, Any]] = []
    docs = list(await Doc.nodes.filter(repository_uid=repository_uid))
    for d in docs:
        c = latest.get(d.uid)
        out.append(
            {
                "scope_uid": d.uid,
                "last_checked": c.checked_at if c else None,
                "revision": (c.revision or "") if c else "",
                "outcome": (c.outcome or "") if c else "",
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
