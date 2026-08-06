"""Comment service — shared by the human API, platform tools, and briefings.

Centralizes DTO conversion, thread listing/creation, and prompt rendering so
the HTTP routes, the executor tool surface, and run-briefing injection all
read and write comments the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from domains.comments import mentions as mention_lib
from domains.comments.models import Comment
from domains.comments.schemas import (
    CommentAuthorKind,
    CommentDTO,
    CommentSubjectType,
    MentionRef,
)
from domains.comments.subjects import (
    get_subjects,
    subject_repository_uid,
    subject_snapshot,
)
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


async def _author_names(comments: list[Comment]) -> dict[str, str]:
    """One-shot display-name lookup for a batch of comments.

    Skips OpenSweep-authored and local-user rows (both resolve without a
    DB round-trip), pools the remaining author_uids into a single
    `User.nodes.filter(uid__in=...)` call, and falls back to the raw uid
    when a user has been deleted. Returns a keyed map so a caller can
    resolve every comment's author with a plain dict lookup instead of an
    await per comment.
    """
    local = get_local_user()
    names: dict[str, str] = {local.uid: local.display_name}
    needed: set[str] = set()
    for c in comments:
        kind = c.author_kind or CommentAuthorKind.USER.value
        if kind == CommentAuthorKind.OPENSWEEP.value:
            continue
        if not c.author_uid or c.author_uid == local.uid:
            continue
        needed.add(c.author_uid)
    if needed:
        users = await User.nodes.filter(uid__in=list(needed))
        for user in users:
            names[user.uid] = user.display_name or user.uid
    return names


def _comment_to_dto_with_names(c: Comment, names: dict[str, str]) -> CommentDTO:
    kind = c.author_kind or CommentAuthorKind.USER.value
    if kind == CommentAuthorKind.OPENSWEEP.value:
        author_name = OPENSWEEP_AUTHOR_NAME
    else:
        author_name = names.get(c.author_uid, c.author_uid)
    return CommentDTO(
        uid=c.uid,
        subject_type=CommentSubjectType(c.subject_type),
        subject_uid=c.subject_uid,
        author_uid=c.author_uid,
        author_name=author_name,
        author_kind=CommentAuthorKind(kind),
        source_run_uid=c.source_run_uid or "",
        body=c.body,
        mentions=[MentionRef(**m) for m in (c.mentions or [])],
        parent_comment_uid=c.parent_comment_uid or "",
        meta=dict(c.meta or {}),
        created_at=c.created_at,
    )


async def comment_to_dto(c: Comment) -> CommentDTO:
    names = await _author_names([c])
    return _comment_to_dto_with_names(c, names)


async def list_comments_for(
    subject_type: CommentSubjectType, subject_uid: str
) -> list[CommentDTO]:
    """Ascending created_at — the reading order of a conversation."""
    nodes = await Comment.nodes.filter(
        subject_type=subject_type.value, subject_uid=subject_uid
    )
    nodes.sort(key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC))
    names = await _author_names(nodes)
    return [_comment_to_dto_with_names(c, names) for c in nodes]


async def create_comment(
    *,
    subject_type: CommentSubjectType,
    subject_uid: str,
    body: str,
    author_uid: str,
    author_kind: CommentAuthorKind = CommentAuthorKind.USER,
    source_run_uid: str = "",
    parent_comment_uid: str = "",
    meta: dict | None = None,
) -> Comment:
    """Persist a comment with its parsed mention refs, and audit it.

    Replies: `parent_comment_uid` must reference a comment on the SAME
    subject; replies-to-replies flatten onto the root parent (one level).
    """
    parent_uid = (parent_comment_uid or "").strip()
    if parent_uid:
        parent = await Comment.nodes.get_or_none(uid=parent_uid)
        if (
            parent is None
            or parent.subject_type != subject_type.value
            or parent.subject_uid != subject_uid
        ):
            raise HTTPException(status_code=404, detail="parent comment not found")
        parent_uid = parent.parent_comment_uid or parent.uid
    c = Comment(
        uid=uuid4().hex,
        subject_type=subject_type.value,
        subject_uid=subject_uid,
        author_uid=author_uid,
        author_kind=author_kind.value,
        source_run_uid=source_run_uid,
        body=body,
        mentions=mention_lib.parse_item_mentions(body),
        parent_comment_uid=parent_uid,
        meta=dict(meta or {}),
        created_at=datetime.now(UTC),
    )
    await c.save()
    # Comment nodes carry no repository_uid — anchor the audit events to the
    # subject's repository explicitly so they are org-visible (audit feed,
    # Slack, notification inbox) instead of platform-level.
    repo_uid = await subject_repository_uid(subject_type, subject_uid) or ""
    await write_audit(
        kind="comment.created",
        subject_uid=c.uid,
        subject_type="Comment",
        actor_uid=author_uid,
        repository_uid=repo_uid,
        payload={
            "comment_subject_type": subject_type.value,
            "comment_subject_uid": subject_uid,
            "author_kind": author_kind.value,
            "mentions": c.mentions,
            "mentions_opensweep": mention_lib.mentions_opensweep(body),
        },
    )
    # `@[Name](user:uid)` tokens land in the mentioned users' notification
    # inboxes — one comment.mention event per mentioned user (not the author).
    for ref in mention_lib.user_mentions(c.mentions or []):
        if ref["uid"] == author_uid:
            continue
        await write_audit(
            kind="comment.mention",
            subject_uid=c.uid,
            subject_type="Comment",
            actor_uid=author_uid,
            repository_uid=repo_uid,
            payload={
                "comment_subject_type": subject_type.value,
                "comment_subject_uid": subject_uid,
                "mentioned_user_uid": ref["uid"],
                "mentioned_user_label": ref.get("label", ""),
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
    ("scheduled_agent_uid", CommentSubjectType.SCHEDULED_AGENT),
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

    Resolution is batched by kind: one Cypher call per mention type in the
    comment rather than one per token, which matters for briefings that
    inline every mention across a long thread.
    """
    if not refs:
        return ""

    # Group uids by kind while preserving the first occurrence order per ref.
    order: list[tuple[str, str]] = []
    uids_by_kind: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        kind, uid = ref.get("type", ""), ref.get("uid", "")
        if not kind or not uid or (kind, uid) in seen:
            continue
        seen.add((kind, uid))
        order.append((kind, uid))
        uids_by_kind.setdefault(kind, []).append(uid)

    resolved: dict[tuple[str, str], Any] = {}
    for kind, uids in uids_by_kind.items():
        if kind == "group":
            from domains.tickets.models import EpicProposal

            groups = await EpicProposal.nodes.filter(uid__in=uids)
            for group in groups:
                resolved[("group", group.uid)] = group
            continue
        try:
            subject_type = CommentSubjectType(kind)
        except ValueError:
            continue
        for subject_uid, subject in (await get_subjects(subject_type, uids)).items():
            resolved[(kind, subject_uid)] = subject

    parts: list[str] = []
    for kind, uid in order:
        node = resolved.get((kind, uid))
        if node is None or node.repository_uid not in allowed_repo_uids:
            continue
        if kind == "group":
            members = ", ".join(node.member_ticket_uids or []) or "(none)"
            parts.append(
                f"- group {uid}: “{node.title}” (status={node.status}, "
                f"member tickets: {members})"
            )
        else:
            subject_type = CommentSubjectType(kind)
            snapshot = subject_snapshot(subject_type, node).replace("\n", "\n  ")
            parts.append(f"- {snapshot}")

    if not parts:
        return ""
    return "Items mentioned in the comment:\n" + "\n".join(parts)
