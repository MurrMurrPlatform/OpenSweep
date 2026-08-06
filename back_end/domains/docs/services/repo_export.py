"""Deterministic docs export — mirror accepted Doc pages into the source
repository as a PR (KNOWLEDGE_V3; the Proposal-3 interop path).

No LLM involved: platform code clones a write sandbox, renders
`AGENTS.md` (marker-delimited block, user content outside the markers is
preserved — the OpenWiki technique) plus one `docs/<slug>.md` per page,
commits, validates, pushes `opensweep/docs-sync`, and opens a PR. Uses the same
write plumbing as implement/fix runs (sandbox_service + write_gate), but no
Run, no agent.

Safety: every managed file carries a first-line marker comment; the mirror
only ever deletes files that carry it, so hand-written docs in the same
tree are never touched. A path assertion (AGENTS.md or docs/** only)
replaces the MergePolicy denylist, which would false-positive on pages
like docs/deployment/terraform.md.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from domains.docs.models import Doc
from logging_config import logger

AGENTS_MD = "AGENTS.md"
DOCS_DIR = "docs"
WORK_BRANCH = "opensweep/docs-sync"

_BLOCK_START = "<!-- OPENSWEEP:START — managed by OpenSweep; edits inside this block are overwritten on sync -->"
_BLOCK_END = "<!-- OPENSWEEP:END -->"
_FILE_MARKER = "<!-- opensweep:doc"


class ExportError(Exception):
    pass


# ---------- pure rendering (unit-testable, no git) ----------


def doc_file_path(slug: str) -> str:
    return f"{DOCS_DIR}/{slug}.md"


def render_doc_file(doc: Doc) -> str:
    watch = ", ".join(doc.watch_paths or [])
    header = f"{_FILE_MARKER} slug={doc.slug} watch_paths={watch!r} -->"
    title = doc.title or doc.slug
    return f"{header}\n# {title}\n\n{(doc.body or '').strip()}\n"


def render_agents_block(docs: list[Doc]) -> str:
    """Pinned pages verbatim + an index of the exported files."""
    pinned = [d for d in docs if d.pinned and (d.body or "").strip()]
    lines: list[str] = [_BLOCK_START, "# Repository documentation (synced from OpenSweep)", ""]
    for d in pinned:
        lines.append(f"## {d.title or d.slug}")
        lines.append("")
        lines.append((d.body or "").strip())
        lines.append("")
    lines.append("## Documentation index")
    lines.append("")
    for d in sorted(docs, key=lambda x: x.slug):
        summary = f" — {d.summary}" if d.summary else ""
        lines.append(f"- [{d.title or d.slug}]({doc_file_path(d.slug)}){summary}")
    lines.append(_BLOCK_END)
    return "\n".join(lines) + "\n"


def merge_agents_md(existing: str, block: str) -> str:
    """Replace the marker block in an existing AGENTS.md, or append it;
    everything outside the markers is the user's and is preserved."""
    if _BLOCK_START in existing and _BLOCK_END in existing:
        before = existing.split(_BLOCK_START, 1)[0]
        after = existing.split(_BLOCK_END, 1)[1]
        return before + block.rstrip("\n") + after
    if existing.strip():
        return existing.rstrip("\n") + "\n\n" + block
    return block


def is_opensweep_managed(content: str) -> bool:
    return content.lstrip().startswith(_FILE_MARKER)


# ---------- the export flow ----------


async def export_docs_to_repo(*, repository_uid: str, actor: str = "") -> dict[str, Any]:
    from domains.delivery.services import write_gate
    from domains.delivery.services.pull_request_service import PullRequestService
    from domains.execution.services.sandbox_service import SandboxService
    from domains.repositories.models import Repository
    from domains.repositories.services.repository_service import repository_to_dto
    from infrastructure.audit import write_audit
    from infrastructure.git_providers import get_git_credentials, get_provider_client

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        raise ExportError(f"repository {repository_uid} not found")
    if not (repo.github_owner and repo.github_repo):
        raise ExportError("docs export requires a GitHub-connected repository")
    token = await get_git_credentials(repo)
    if not token:
        raise ExportError(
            "no GitHub credential (connect the GitHub App or set GITHUB_TOKEN)"
        )

    docs = [
        d
        for d in await Doc.nodes.filter(repository_uid=repository_uid, archived=False)
        if (d.body or "").strip()
    ]
    if not docs:
        raise ExportError("no documentation pages with content to export")

    client = get_provider_client(repo)
    default_branch = repo.default_branch or "main"
    existing_branch = await client.get_branch(
        repo.github_owner, repo.github_repo, WORK_BRANCH
    )

    dto = repository_to_dto(repo)
    sandbox = await SandboxService().create_for_write(
        repository=dto,
        agent_run_uid="docs-sync",
        work_branch=WORK_BRANCH,
        base_branch=default_branch,
        checkout_existing=existing_branch is not None,
    )
    root = sandbox.container_path
    try:
        # Render the mirror.
        expected: dict[str, str] = {doc_file_path(d.slug): render_doc_file(d) for d in docs}
        for rel, content in expected.items():
            abs_path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

        # Delete previously-synced pages that no longer exist (marker-guarded:
        # never touch files OpenSweep did not write).
        docs_root = os.path.join(root, DOCS_DIR)
        removed: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(docs_root):
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, root)
                if rel in expected:
                    continue
                try:
                    with open(abs_path, encoding="utf-8") as f:
                        head = f.read(200)
                except OSError:
                    continue
                if is_opensweep_managed(head):
                    os.remove(abs_path)
                    removed.append(rel)

        # AGENTS.md marker block.
        agents_path = os.path.join(root, AGENTS_MD)
        existing = ""
        if os.path.exists(agents_path):
            with open(agents_path, encoding="utf-8") as f:
                existing = f.read()
        with open(agents_path, "w", encoding="utf-8") as f:
            f.write(merge_agents_md(existing, render_agents_block(docs)))

        # Stage only what this export manages; bail cleanly when nothing moved.
        await write_gate._git(root, "add", "--", AGENTS_MD, DOCS_DIR)
        status = await write_gate._git(root, "status", "--porcelain")
        if not status.strip():
            return {
                "status": "no_changes",
                "pages": len(docs),
                "detail": "repository already matches the documentation",
            }
        await write_gate._git(
            root, "commit", "-m", "docs: sync documentation from OpenSweep"
        )

        # The gate's denylist would false-positive on docs/deployment/*.md —
        # path safety here is the stronger assertion below instead.
        base_ref = WORK_BRANCH if existing_branch is not None else default_branch

        class _NoDenylistPolicy:
            path_denylist: list[str] = []

        result = await write_gate.validate_sandbox_changes(
            root,
            base_ref=base_ref,
            policy=_NoDenylistPolicy(),
            default_branch=default_branch,
        )
        if not result.ok:
            raise ExportError("; ".join(result.violations))
        offenders = [
            p
            for p in result.changed_paths
            if p != AGENTS_MD and not p.startswith(DOCS_DIR + "/")
        ]
        if offenders:
            raise ExportError(f"export touched unexpected paths: {offenders[:5]}")

        await write_gate.push_work_branch(
            root, work_branch=WORK_BRANCH, token=token, default_branch=default_branch
        )

        # Open (or adopt) the PR.
        body = (
            "Mirrors OpenSweep's accepted documentation pages into the repository: "
            f"`{AGENTS_MD}` (marker block) plus `{DOCS_DIR}/` "
            f"({len(docs)} page(s){f', {len(removed)} removed' if removed else ''}).\n\n"
            + "\n".join(f"- {doc_file_path(d.slug)}" for d in sorted(docs, key=lambda x: x.slug))
            + "\n\n_Generated by OpenSweep docs sync — no agent involved._\n"
        )
        try:
            payload = await client.open_pull_request(
                repo.github_owner,
                repo.github_repo,
                head=WORK_BRANCH,
                base=default_branch,
                title="docs: sync documentation from OpenSweep",
                body=body,
                draft=False,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:
                raise
            prs = await client.list_pull_requests(
                repo.github_owner, repo.github_repo, state="open"
            )
            payload = next(
                (p for p in prs if ((p.get("head") or {}).get("ref")) == WORK_BRANCH),
                None,
            )
            if payload is None:
                raise
        pr = await PullRequestService().upsert_from_payload(repo, payload)

        await write_audit(
            kind="docs.exported",
            subject_uid=repository_uid,
            subject_type="Repository",
            actor_uid=actor or "docs-sync",
            payload={
                "pages": len(docs),
                "removed": removed,
                "pr_number": payload.get("number"),
            },
        )
        return {
            "status": "ok",
            "pages": len(docs),
            "removed": removed,
            "pull_request_uid": pr.uid,
            "pr_number": payload.get("number"),
            "pr_url": payload.get("html_url") or "",
            "synced_at": datetime.now(UTC).isoformat(),
        }
    finally:
        try:
            await SandboxService().destroy(sandbox.uid, actor_uid="docs-sync")
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the result
            logger.warning(
                f"docs export: sandbox cleanup failed: {exc}", extra={"tag": "docs"}
            )
