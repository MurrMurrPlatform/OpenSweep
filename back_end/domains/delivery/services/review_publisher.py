"""Publish a Verdict to GitHub as a real review — summary + inline comments.

Before this, the only thing that reached GitHub was the single
`opensweep/converged` commit status (`pull_request_service._publish_status`):
a reviewer on github.com saw a green or red check and none of the reasoning.
Every finding, per-criterion result and blocking judgment stayed inside
OpenSweep behind a click-out.

Anchoring rules (why this is fussy):
  - GitHub rejects the ENTIRE review with 422 if any inline comment points at
    a line that is not part of the diff. So comments are filtered to the PR's
    changed paths, and `publish_verdict_review` falls back to a body-only
    review if GitHub rejects the batch anyway.
  - Findings carry `affected_paths: list[str]` using a documented "path:line"
    convention (see agents/services/seed_agent_bases.py). Paths without a
    parseable line, or outside the diff, degrade into the summary body rather
    than being dropped.

The review event is COMMENT, never APPROVE / REQUEST_CHANGES — see
`GitHubClient.create_review`.
"""

from __future__ import annotations

import re

from domains.delivery.services.convergence import severity_blocks
from logging_config import logger

#: "path/to/file.py:120" or "path/to/file.py:120-134" → (path, first line).
#: Windows-style drive letters are not a concern (repo-relative paths only).
_PATH_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:-\d+)?$")

#: GitHub silently truncates very long review bodies; keep well clear.
_MAX_BODY = 60_000

#: A review with hundreds of inline comments is noise, and GitHub rate-limits
#: large batches. Overflow is summarized in the body instead.
_MAX_INLINE_COMMENTS = 30

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}


def parse_path_line(raw: str) -> tuple[str, int | None]:
    """Split an `affected_paths` entry into (path, line or None)."""
    text = (raw or "").strip()
    if not text:
        return "", None
    m = _PATH_LINE.match(text)
    if not m:
        return text, None
    return m.group("path"), int(m.group("line"))


def _anchor_for(finding: dict, changed_paths: set[str]) -> tuple[str, int] | None:
    """First (path, line) of this finding that lands inside the diff."""
    for raw in finding.get("affected_paths") or []:
        path, line = parse_path_line(str(raw))
        if path and line and path in changed_paths:
            return path, line
    return None


def _finding_comment_body(finding: dict) -> str:
    severity = str(finding.get("severity") or "medium")
    emoji = _SEVERITY_EMOJI.get(severity, "🟡")
    parts = [f"{emoji} **{severity.upper()}** — {finding.get('title') or 'Finding'}"]
    if finding.get("why_it_matters"):
        parts.append(str(finding["why_it_matters"]))
    elif finding.get("description"):
        parts.append(str(finding["description"]))
    if finding.get("suggested_fix"):
        parts.append(f"**Suggested fix:** {finding['suggested_fix']}")
    return "\n\n".join(p for p in parts if p)


def build_review(
    *,
    verdict: dict,
    findings: list[dict],
    changed_paths: set[str],
    blocking_policy: dict,
    convergence_summary: str = "",
    detail_url: str = "",
) -> tuple[str, list[dict]]:
    """Render (body, inline_comments) for one verdict. Pure — no I/O."""
    result = str(verdict.get("result") or "")
    blocking = int(verdict.get("new_blocking_findings") or 0)

    header = {
        "approve": "✅ **OpenSweep review: approved**",
        "request_changes": "🔧 **OpenSweep review: changes requested**",
        "needs_human": "🙋 **OpenSweep review: needs a human**",
    }.get(result, f"**OpenSweep review: {result or 'unknown'}**")

    lines: list[str] = [header, ""]
    if convergence_summary:
        lines += [convergence_summary, ""]
    lines.append(
        f"{len(findings)} finding(s) raised · {blocking} blocking under this "
        "repository's merge policy."
    )

    inline: list[dict] = []
    unanchored: list[dict] = []
    for finding in findings:
        anchor = _anchor_for(finding, changed_paths)
        if anchor and len(inline) < _MAX_INLINE_COMMENTS:
            path, line = anchor
            inline.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": _finding_comment_body(finding),
                }
            )
        else:
            unanchored.append(finding)

    if unanchored:
        # Findings that touch files outside the diff (or carry no line) still
        # have to be visible — they are often the most important ones, since a
        # missing test or a wrong config lives outside the changed lines.
        lines += ["", "### Not anchored to changed lines", ""]
        for finding in unanchored:
            severity = str(finding.get("severity") or "medium")
            emoji = _SEVERITY_EMOJI.get(severity, "🟡")
            blocks = severity_blocks(
                severity, list(finding.get("tags") or []), blocking_policy
            )
            where = ", ".join(
                f"`{p}`" for p in (finding.get("affected_paths") or [])[:3]
            )
            suffix = " **(blocking)**" if blocks else ""
            lines.append(
                f"- {emoji} **{finding.get('title') or 'Finding'}**{suffix}"
                + (f" — {where}" if where else "")
            )

    ac_results = verdict.get("ac_results") or []
    if ac_results:
        lines += ["", "### Acceptance criteria", ""]
        mark = {"pass": "✅", "fail": "❌", "unverifiable": "❔"}
        for ac in ac_results:
            symbol = mark.get(str(ac.get("result") or ""), "❔")
            lines.append(f"- {symbol} {ac.get('criterion') or ''}")

    if detail_url:
        lines += ["", f"[Full review ledger in OpenSweep]({detail_url})"]

    return "\n".join(lines)[:_MAX_BODY], inline


async def publish_verdict_review(
    *, repo, pr, verdict, findings: list[dict], convergence_summary: str = ""
) -> bool:
    """Post `verdict` to GitHub as a review. Returns True when something was
    published.

    Never raises: a GitHub outage must not fail the verdict that was already
    persisted — same posture as `_publish_status`.
    """
    from config import settings
    from domains.delivery.services.resolution_service import ensure_merge_policy
    from infrastructure.git_providers import get_provider_client

    if not pr.github_number or pr.state != "open":
        return False
    client = get_provider_client(repo)
    if not client.is_active or not (repo.github_owner and repo.github_repo):
        return False

    changed_paths: set[str] = set()
    try:
        files = await client.list_pull_request_files(
            repo.github_owner, repo.github_repo, pr.github_number
        )
        changed_paths = {str(f.get("filename") or "") for f in files}
    except Exception as exc:  # noqa: BLE001 — degrade to a body-only review
        logger.warning(
            f"review publish: could not list files for {pr.pr_key}: {exc}",
            extra={"tag": "delivery"},
        )

    policy = await ensure_merge_policy(pr.repository_uid)
    body, inline = build_review(
        verdict=verdict,
        findings=findings,
        changed_paths=changed_paths,
        blocking_policy=dict(policy.blocking or {}),
        convergence_summary=convergence_summary,
        detail_url=(
            f"{settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL}/pull-requests/{pr.uid}"
        ),
    )

    try:
        await client.create_review(
            repo.github_owner,
            repo.github_repo,
            pr.github_number,
            body=body,
            event="COMMENT",
            commit_id=str(verdict.get("sha") or ""),
            comments=inline,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Most likely a 422 from an inline comment outside the diff. Retry
        # without anchors so the reasoning still reaches the PR.
        logger.warning(
            f"review publish with {len(inline)} inline comment(s) failed for "
            f"{pr.pr_key}: {exc} — retrying body-only",
            extra={"tag": "delivery"},
        )

    try:
        await client.create_review(
            repo.github_owner,
            repo.github_repo,
            pr.github_number,
            body=body,
            event="COMMENT",
            commit_id=str(verdict.get("sha") or ""),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"review publish failed for {pr.pr_key}: {exc}",
            extra={"tag": "delivery"},
        )
        return False
