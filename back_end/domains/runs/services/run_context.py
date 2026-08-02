"""Run conversation briefing — what the in-chat agent knows before turn one.

Ported from V2's session_context. A chat run created from a PR/ticket/finding
carries only link uids on the node; without this preamble the CLI agent
starts blind ("do you see the ticket?" → it greps around). Rendered from the
linked entities and injected on EVERY turn (claude: --append-system-prompt;
opencode: prompt prefix), so context survives CLI session rotation and resume.
"""

from __future__ import annotations

# Per-section ceiling: one oversized entity (a pasted-log ticket description)
# must not starve the sections after it.
_MAX_SECTION_CHARS = 1800


def _cap(text: str, limit: int = _MAX_SECTION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 12] + "\n[truncated]"


def render_run_context(
    *,
    run_uid: str = "",
    repo=None,
    pr=None,
    ticket=None,
    finding=None,
    resolutions=(),
) -> str:
    """Pure renderer — entities in, compact briefing out. Any input may be None."""
    parts: list[str] = [
        "You are chatting with a maintainer inside OpenSweep, an AI dev-workflow platform.",
        "Your working directory is a disposable sandbox clone of the repository — read "
        "code freely; do not push. OpenSweep platform tools (opensweep_platform_*) are available "
        "for the findings/PR/ticket ledger; opensweep_platform_list_comments shows human "
        "instructions and opensweep_platform_add_comment replies on a thread.",
    ]
    ids: list[str] = []
    if run_uid:
        ids.append(f"- run_uid: {run_uid}")
    if repo is not None and getattr(repo, "uid", ""):
        ids.append(f"- repository_uid: {repo.uid}")
    if pr is not None and getattr(pr, "uid", ""):
        ids.append(f"- pull_request_uid: {pr.uid}")
    if ticket is not None and getattr(ticket, "uid", ""):
        ids.append(f"- ticket_uid: {ticket.uid}")
    if finding is not None and getattr(finding, "uid", ""):
        ids.append(f"- finding_uid: {finding.uid}")
    if ids:
        parts.append(
            "Identifiers — opensweep_platform_* tools take these uids as explicit "
            "parameters; use them instead of searching:\n" + "\n".join(ids)
        )
    if repo is not None:
        parts.append(
            f"Repository: {getattr(repo, 'github_owner', '')}/{getattr(repo, 'github_repo', '')}"
            f" (default branch {getattr(repo, 'default_branch', 'main')})."
        )
    if pr is not None:
        conv = getattr(pr, "convergence", None) or {}
        counts = conv.get("counts") or {}
        reasons = conv.get("reasons") or []
        parts.append(
            "This conversation is about PULL REQUEST "
            f"#{getattr(pr, 'github_number', '?')}: \"{getattr(pr, 'title', '')}\"\n"
            f"- branch {getattr(pr, 'head_ref', '?')} → {getattr(pr, 'base_ref', '?')}, "
            f"state {getattr(pr, 'state', '?')}, head {str(getattr(pr, 'head_sha', ''))[:12]}\n"
            f"- convergence: {'CONVERGED' if getattr(pr, 'converged', False) else 'not converged'}"
            f" ({counts.get('blocking', 0)} blocking / {counts.get('deferred', 0)} deferred / "
            f"{counts.get('waived', 0)} waived / {counts.get('info', 0)} info)\n"
            + (f"- outstanding: {'; '.join(str(r) for r in reasons[:4])}" if reasons else "")
        )
    if resolutions:
        lines = []
        for r in list(resolutions)[:8]:
            lines.append(
                f"  - [{getattr(r, 'state', '?')}{' · BLOCKING' if getattr(r, 'blocking', False) else ''}] "
                f"{getattr(r, 'finding_title', '') or getattr(r, 'finding_uid', '')} "
                f"({getattr(r, 'finding_severity', '?')}"
                f"{'/' + ','.join(getattr(r, 'finding_tags', None) or []) if getattr(r, 'finding_tags', None) else ''}"
                f", finding_uid {getattr(r, 'finding_uid', '') or '?'})"
            )
        parts.append("Findings on this PR:\n" + "\n".join(lines))
    if ticket is not None:
        criteria = [str(c) for c in (getattr(ticket, "acceptance_criteria", None) or []) if str(c).strip()]
        ac = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria)) or "  (none recorded)"
        parts.append(
            "This conversation is about TICKET "
            f"\"{getattr(ticket, 'title', '')}\" (status {getattr(ticket, 'status', '?')}, "
            f"priority {getattr(ticket, 'priority', '?')}).\n"
            f"Description: {(getattr(ticket, 'description', '') or '(none)')[:600]}\n"
            f"Acceptance criteria:\n{ac}"
        )
    if finding is not None:
        paths = ", ".join((getattr(finding, "affected_paths", None) or [])[:5]) or "n/a"
        parts.append(
            "This conversation is about FINDING "
            f"\"{getattr(finding, 'title', '')}\" "
            f"({getattr(finding, 'severity', '?')}"
            f"{'/' + ','.join(getattr(finding, 'tags', None) or []) if getattr(finding, 'tags', None) else ''}, "
            f"status {getattr(finding, 'status', '?')}).\n"
            f"Why it matters: {(getattr(finding, 'why_it_matters', '') or '(not recorded)')[:400]}\n"
            f"Suggested fix: {(getattr(finding, 'suggested_fix', '') or '(not recorded)')[:400]}\n"
            f"Affected paths: {paths}"
        )
    return "\n\n".join(_cap(p) for p in parts if p.strip())


async def build_run_context(run, *, timeout_seconds: float = 5.0) -> str:
    """Fetch the run's linked entities and render the briefing.

    Hard-bounded: a slow/unreachable database yields a generic briefing
    instead of blocking the turn — context is best-effort, never turn-fatal.
    """
    import asyncio

    try:
        return await asyncio.wait_for(_build(run), timeout=timeout_seconds)
    except (TimeoutError, Exception):  # noqa: BLE001
        return render_run_context()


async def _build(run) -> str:
    repo = pr = ticket = finding = None
    resolutions: list = []
    try:
        from domains.repositories.models import Repository

        repo = await Repository.nodes.get_or_none(uid=run.repository_uid)
    except Exception:  # noqa: BLE001 — context is best-effort, never turn-fatal
        pass
    if getattr(run, "linked_pr_uid", ""):
        try:
            from domains.delivery.models import PullRequest
            from domains.delivery.services.resolution_service import ResolutionService

            pr = await PullRequest.nodes.get_or_none(uid=run.linked_pr_uid)
            if pr is not None:
                resolutions = await ResolutionService().list_for_pr(pr.uid)
        except Exception:  # noqa: BLE001
            pass
    if getattr(run, "linked_ticket_uid", ""):
        try:
            from domains.tickets.models import Ticket

            ticket = await Ticket.nodes.get_or_none(uid=run.linked_ticket_uid)
        except Exception:  # noqa: BLE001
            pass
    if getattr(run, "linked_finding_uid", ""):
        try:
            from domains.findings.models import Finding

            finding = await Finding.nodes.get_or_none(uid=run.linked_finding_uid)
        except Exception:  # noqa: BLE001
            pass
    return render_run_context(
        run_uid=run.uid,
        repo=repo,
        pr=pr,
        ticket=ticket,
        finding=finding,
        resolutions=resolutions,
    )
