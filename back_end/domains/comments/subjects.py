"""Comment-subject registry — one place that knows which node backs each
CommentSubjectType and how to describe it in prompts.

Every subject model exposes `uid`, `repository_uid`, and `title`, so tenancy
checks and prompt snapshots stay generic. Imports are lazy: the comments
domain must not create import cycles with every other domain.
"""

from __future__ import annotations

from typing import Any

from domains.comments.schemas import CommentSubjectType


def _model_for(subject_type: CommentSubjectType) -> Any:
    if subject_type == CommentSubjectType.FINDING:
        from domains.findings.models import Finding

        return Finding
    if subject_type == CommentSubjectType.TICKET:
        from domains.tickets.models import Ticket

        return Ticket
    if subject_type == CommentSubjectType.PULL_REQUEST:
        from domains.delivery.models import PullRequest

        return PullRequest
    if subject_type == CommentSubjectType.NEWS_ITEM:
        from domains.news.models import NewsItem

        return NewsItem
    if subject_type == CommentSubjectType.RUN:
        from domains.runs.models import Run

        return Run
    if subject_type == CommentSubjectType.SCHEDULED_AGENT:
        from domains.agents.models import ScheduledAgent

        return ScheduledAgent
    from domains.docs.models import Doc

    return Doc


async def get_subject(subject_type: CommentSubjectType, subject_uid: str) -> Any | None:
    """The subject node, or None when it doesn't exist."""
    return await _model_for(subject_type).nodes.get_or_none(uid=subject_uid)


async def subject_repository_uid(
    subject_type: CommentSubjectType, subject_uid: str
) -> str | None:
    """The subject's repository_uid (tenancy anchor), or None when missing."""
    subject = await get_subject(subject_type, subject_uid)
    return subject.repository_uid if subject is not None else None


def subject_snapshot(subject_type: CommentSubjectType, subject: Any) -> str:
    """Compact prompt-ready description of the item a thread hangs on."""
    lines = [
        f"Type: {subject_type.value}",
        f"uid: {subject.uid}",
        f"Title: {getattr(subject, 'title', '') or '(untitled)'}",
    ]
    if subject_type == CommentSubjectType.FINDING:
        lines += [
            f"Kind: {getattr(subject, 'kind', '')}",
            f"Severity: {getattr(subject, 'severity', '')}",
            f"Status: {getattr(subject, 'status', '')}",
            f"Description: {getattr(subject, 'description', '')}",
        ]
    elif subject_type == CommentSubjectType.TICKET:
        criteria = "\n".join(f"  - {c}" for c in (subject.acceptance_criteria or []))
        lines += [
            f"Status: {subject.status}",
            f"Priority: {subject.priority}",
            f"Description: {subject.description}",
            f"Acceptance criteria:\n{criteria or '  - (none)'}",
        ]
    elif subject_type == CommentSubjectType.PULL_REQUEST:
        lines += [
            f"GitHub: #{subject.github_number} ({subject.url})",
            f"State: {subject.state} (draft={bool(subject.draft)})",
            f"Branch: {subject.head_ref} → {subject.base_ref}",
        ]
    elif subject_type == CommentSubjectType.NEWS_ITEM:
        lines += [
            f"Category: {subject.category}",
            f"URL: {subject.url}",
            f"Summary: {subject.summary}",
            f"Relevance: {subject.relevance}",
        ]
    elif subject_type == CommentSubjectType.RUN:
        lines += [
            f"Playbook: {subject.playbook}",
            f"Status: {subject.status}",
        ]
    elif subject_type == CommentSubjectType.SCHEDULED_AGENT:
        lines += [f"Trigger: {getattr(subject, 'trigger', '')}"]
    elif subject_type == CommentSubjectType.DOC:
        lines += [
            f"Slug: {subject.slug}",
            f"Summary: {subject.summary}",
        ]
    return "\n".join(line for line in lines if line.strip())
