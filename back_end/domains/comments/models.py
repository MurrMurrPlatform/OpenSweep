"""Comment — a note pinned to any OpenSweep data item.

Comments are the conversation layer of the platform: maintainers leave
instructions ("waive this one", "fix the root cause, not the symptom") on
findings, tickets, pull requests, news items, runs, scheduled agents, and docs.
Agents read the thread (it is injected into run briefings and available via
`opensweep_platform_list_comments`) and — when summoned with an `@opensweep` mention —
reply in-thread through `opensweep_platform_add_comment`.

Mentions live inline in the body:
  - `@opensweep` summons a background run that reads the thread and responds.
  - `@[Label](type:uid)` references another data item (ticket, finding, …);
    the parsed refs are denormalized into `mentions` for validation and
    prompt context.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)


class Comment(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    subject_type = StringProperty(required=True, index=True)
    # finding | ticket | pull_request | news_item | run | scheduled_agent | doc
    subject_uid = StringProperty(required=True, index=True)

    author_uid = StringProperty(required=True)
    author_kind = StringProperty(default="user")  # user | opensweep
    source_run_uid = StringProperty(default="", index=True)
    body = StringProperty(required=True)
    # Denormalized data-item refs parsed from `@[Label](type:uid)` tokens.
    mentions = JSONProperty(default=[])  # [{type, uid, label}]

    # Reply threading (one level): uid of the top-level comment this replies
    # to. "" = top-level. Replies-to-replies flatten onto the root parent.
    parent_comment_uid = StringProperty(default="", index=True)
    # Machine metadata for platform-authored comments — e.g. thread-question
    # mirrors: {kind: "thread_question", thread_uid, question_uid, options,
    # status: open|answered}. Drives quick-reply chips in the UI.
    meta = JSONProperty(default={})

    created_at = DateTimeProperty(default_now=True)


COMMENT_SUBJECT_TYPES = {
    "finding",
    "ticket",
    "pull_request",
    "news_item",
    "run",
    "scheduled_agent",
    "doc",
}
