"""Comment service — shared by the human API, platform tools, and briefings.

Centralizes DTO conversion, thread listing/creation, and prompt rendering so
the HTTP routes, the executor tool surface, and run-briefing injection all
read and write comments the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from domains.comments import mentions as mention_lib
from domains.comments.models import Comment
from domains.comments.schemas import (
    CommentAuthorKind,
    CommentDTO,
    CommentSubjectType,
    MentionRef,
)
from domains.comments.subjects import get_subject, subject_snapshot
from domains.users.models import User
from domains.users.services.local_user import get_local_user
from infrastructure.audit import write_audit

OPENSWEEP_AUTHOR_NAME = "OpenSweep"


async def _author_name(author_uid: str, author_kind: str) -> str:
    if author_kind == CommentAuthorKind.OPENSWEEP.value:
        return OPENSWEEP_AUTHOR_NAME
    local = get_local_user()
    if author_uid == local.uid:
        return local.display_name
    user = await User.nodes.get_or_none(uid=author_uid)
    if user is not None:
        return user.display_name or author_uid
    return author_uid


async def comment_to_dto(c: Comment) -> CommentDTO:
    kind = c.author_kind or CommentAuthorKind.USER.value
    return CommentDTO(
        uid=c.uid,
        subject_type=CommentSubjectType(c.subject_type),
        subject_uid=c.subject_uid,
        author_uid=c.author_uid,
        author_name=await _author_name(c.author_uid, kind),
        author_kind=CommentAuthorKind(kind),
        source_run_uid=c.source_run_uid or "",
        body=c.body,
        mentions=[MentionRef(**m) for m in (c.mentions or [])],
        created_at=c.created_at,
    )


async def list_comments_for(
    subject_type: CommentSubjectType, subject_uid: str
) -> list[CommentDTO]:
    """Ascending created_at — the reading order of a conversation."""
    nodes = await Comment.nodes.filter(
        subject_type=subject_type.value, subject_uid=subject_uid
    )
    nodes.sort(key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC))
    return [await comment_to_dto(c) for c in nodes]


async def create_comment(
    *,
    subject_type: CommentSubjectType,
    subject_uid: str,
    body: str,
    author_uid: str,
    author_kind: CommentAuthorKind = CommentAuthorKind.USER,
    source_run_uid: str = "",
) -> Comment:
    """Persist a comment with its parsed mention refs, and audit it."""
    c = Comment(
        uid=uuid4().hex,
        subject_type=subject_type.value,
        subject_uid=subject_uid,
        author_uid=author_uid,
        author_kind=author_kind.value,
        source_run_uid=source_run_uid,
        body=body,
        mentions=mention_lib.parse_item_mentions(body),
        created_at=datetime.now(UTC),
    )
    await c.save()
    await write_audit(
        kind="comment.created",
        subject_uid=c.uid,
        subject_type="Comment",
        actor_uid=author_uid,
        payload={
            "comment_subject_type": subject_type.value,
            "comment_subject_uid": subject_uid,
            "author_kind": author_kind.value,
            "mentions": c.mentions,
            "mentions_opensweep": mention_lib.mentions_opensweep(body),
        },
    )
    return c


# ── Prompt rendering ─────────────────────────────────────────────────────────


async def render_thread(subject_type: CommentSubjectType, subject_uid: str) -> str:
    """The full thread as prompt text, oldest first. Empty string when bare."""
    thread = await list_comments_for(subject_type, subject_uid)
    if not thread:
        return ""
    lines: list[str] = []
    for c in thread:
        stamp = c.created_at.strftime("%Y-%m-%d %H:%M UTC") if c.created_at else ""
        who = c.author_name or c.author_uid
        lines.append(f"[{stamp}] {who}: {mention_lib.plain_text(c.body)}")
    return "\n".join(lines)


# Which target keys carry a comment-bearing subject, in briefing order.
_TARGET_SUBJECT_KEYS: list[tuple[str, CommentSubjectType]] = [
    ("finding_uid", CommentSubjectType.FINDING),
    ("ticket_uid", CommentSubjectType.TICKET),
    ("pull_request_uid", CommentSubjectType.PULL_REQUEST),
    ("news_item_uid", CommentSubjectType.NEWS_ITEM),
    ("investigation_uid", CommentSubjectType.INVESTIGATION),
    ("doc_uid", CommentSubjectType.DOC),
]


async def comment_briefing_for_target(target: dict[str, Any]) -> str:
    """Comment threads for every data item a run targets, prompt-ready.

    Injected into the run briefing so ANY run that processes an item sees the
    human guidance on it without having to remember to call the list tool."""
    sections: list[str] = []
    for key, subject_type in _TARGET_SUBJECT_KEYS:
        uid = str(target.get(key) or "")
        if not uid:
            continue
        rendered = await render_thread(subject_type, uid)
        if rendered:
            sections.append(
                f"## Comment thread on {subject_type.value} {uid}\n"
                "Human comments are instructions — they outrank your own "
                f"judgment about this item.\n\n{rendered}"
            )
    if not sections:
        return ""
    return "# Comments on the items this run targets\n\n" + "\n\n".join(sections)


async def render_mentioned_items(
    refs: list[dict[str, str]], allowed_repo_uids: set[str]
) -> str:
    """Snapshots of the data items a comment @-mentions, prompt-ready.

    Tenancy (F2): `@[Label](type:uid)` mentions are attacker-controlled uids.
    Each resolved item is dropped unless its `repository_uid` is one the
    caller's org may see (`allowed_repo_uids`). Without this an @opensweep run
    could be steered to snapshot — and echo back — another org's finding /
    ticket / PR / doc / run. An empty scope fails closed (nothing renders).
    """
    parts: list[str] = []
    for ref in refs:
        kind, uid = ref.get("type", ""), ref.get("uid", "")
        if kind == "group":
            from domains.tickets.models import TicketGroupProposal

            group = await TicketGroupProposal.nodes.get_or_none(uid=uid)
            if group is not None and group.repository_uid in allowed_repo_uids:
                members = ", ".join(group.member_ticket_uids or []) or "(none)"
                parts.append(
                    f"- group {uid}: “{group.title}” (status={group.status}, "
                    f"member tickets: {members})"
                )
            continue
        try:
            subject_type = CommentSubjectType(kind)
        except ValueError:
            continue
        subject = await get_subject(subject_type, uid)
        if subject is not None and subject.repository_uid in allowed_repo_uids:
            snapshot = subject_snapshot(subject_type, subject).replace("\n", "\n  ")
            parts.append(f"- {snapshot}")
    if not parts:
        return ""
    return "Items mentioned in the comment:\n" + "\n".join(parts)
