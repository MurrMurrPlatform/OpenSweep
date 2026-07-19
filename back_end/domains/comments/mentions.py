"""Mention parsing — pure functions over comment bodies.

Two mention forms live inline in a comment body:
  - `@opensweep` (word-boundary, case-insensitive) summons the platform agent.
  - `@[Label](type:uid)` references another data item; `type` is a
    CommentSubjectType value, `group` (a TicketGroupProposal), or `user`
    (a User — records a `comment.mention` notification for that user).
"""

from __future__ import annotations

import re

OPENSWEEP_MENTION_RE = re.compile(r"(?<!\w)@opensweep\b", re.IGNORECASE)

# @[Fix login flakiness](ticket:a1b2c3…) — label may hold anything but `]`.
ITEM_MENTION_RE = re.compile(r"@\[([^\]]+)\]\((\w+):([A-Za-z0-9_-]+)\)")

# Every type addressable from the composer. Superset of the comment subject
# types: `group` mentions a TicketGroupProposal, which carries no thread;
# `user` mentions a person and lands in their notification inbox.
MENTIONABLE_TYPES = {
    "finding",
    "ticket",
    "pull_request",
    "news_item",
    "run",
    "scheduled_agent",
    "doc",
    "group",
    "user",
}


def user_mentions(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    """The `user`-type refs among parsed mentions — the people to notify."""
    return [ref for ref in refs if ref.get("type") == "user" and ref.get("uid")]


def mentions_opensweep(body: str) -> bool:
    return bool(OPENSWEEP_MENTION_RE.search(body or ""))


def parse_item_mentions(body: str) -> list[dict[str, str]]:
    """Ordered, de-duplicated `{type, uid, label}` refs from body tokens.

    Unknown types are dropped rather than rejected — a stale client can't
    poison the comment, and the raw token stays readable in the body."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in ITEM_MENTION_RE.finditer(body or ""):
        label, kind, uid = match.group(1).strip(), match.group(2), match.group(3)
        if kind not in MENTIONABLE_TYPES or (kind, uid) in seen:
            continue
        seen.add((kind, uid))
        out.append({"type": kind, "uid": uid, "label": label})
    return out


def plain_text(body: str) -> str:
    """Body with mention tokens flattened to prompt-friendly references."""

    def _flatten(match: re.Match[str]) -> str:
        label, kind, uid = match.group(1).strip(), match.group(2), match.group(3)
        return f"{label} ({kind} {uid})"

    return ITEM_MENTION_RE.sub(_flatten, body or "")
