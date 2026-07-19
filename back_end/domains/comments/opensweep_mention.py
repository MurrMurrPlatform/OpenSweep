"""@opensweep mention handling — a comment summons a background run.

When a saved comment mentions @opensweep, we dispatch a refine run (read-only
against the repository, full platform-tool access) whose intent carries the
item snapshot, the entire comment thread, and any @-mentioned data items.
The run's contract is to act on the request and ALWAYS answer in-thread via
`opensweep_platform_add_comment`.

Dispatch is best-effort: the comment must save even when no provider is
configured — the failure is surfaced in the API response instead.
"""

from __future__ import annotations

from typing import Any

from domains.comments import mentions as mention_lib
from domains.comments.models import Comment
from domains.comments.schemas import CommentSubjectType
from domains.comments.service import render_mentioned_items, render_thread
from domains.comments.subjects import subject_snapshot
from domains.runs.schemas import Effort, RunTrigger
from domains.run_policies.services.effort import ensure_policy_for_effort
from logging_config import logger

# Subject types whose uid also lands on the Run's linked_* columns, which the
# UI uses to surface "runs about this item".
_LINK_FIELD = {
    CommentSubjectType.FINDING: "linked_finding_uid",
    CommentSubjectType.TICKET: "linked_ticket_uid",
    CommentSubjectType.PULL_REQUEST: "linked_pr_uid",
}


def build_opensweep_comment_intent(
    subject_type: CommentSubjectType,
    subject: Any,
    comment: Comment,
    *,
    thread: str,
    mentioned: str,
) -> str:
    """First-turn prompt for a comment-summoned run."""
    author = comment.author_uid
    parts = [
        "A maintainer summoned you with an @opensweep mention in a comment. Read "
        "their request, act on it with the platform tools, and reply on the "
        "same thread.",
        f"## The item the comment is on\n{subject_snapshot(subject_type, subject)}",
        f"## The comment that summoned you (author {author}, comment uid {comment.uid})\n"
        f"{mention_lib.plain_text(comment.body)}",
    ]
    if mentioned:
        parts.append(f"## {mentioned}")
    if thread:
        parts.append(
            "## Full comment thread (oldest first) — context for the request\n" + thread
        )
    parts.append(
        "## Your task\n"
        "1. Interpret the request in the summoning comment. Study the item and "
        "any code it points at (the repository clone is read-only — never "
        "modify code).\n"
        "2. Perform the request with the opensweep_platform_* tools (for example "
        "opensweep_platform_update_ticket, opensweep_platform_update_finding, "
        "opensweep_platform_propose_ticket_group).\n"
        "3. REQUIRED: reply on the thread by calling opensweep_platform_add_comment "
        f"with subject_type={subject_type.value!r} and "
        f"subject_uid={subject.uid!r}. Summarize what you did (or why you "
        "could not) in a concise, maintainer-facing comment. Do not finish "
        "without posting this reply.\n"
        "4. Call opensweep_platform_complete_run when done."
    )
    return "\n\n".join(parts)


def filter_pending_for_subject(
    runs: list[Any], subject_type: CommentSubjectType, subject_uid: str
) -> list[Any]:
    """The comment-surface runs whose target points at this thread's subject."""
    return [
        r
        for r in runs
        if (r.target or {}).get("subject_type") == subject_type.value
        and (r.target or {}).get("subject_uid") == subject_uid
    ]


async def pending_opensweep_runs(
    subject_type: CommentSubjectType, subject_uid: str
) -> list[Any]:
    """In-flight @opensweep reply runs for a thread (queued/running/paused_quota).

    Backs the thread's thinking bubble across page reloads. awaiting_input
    and the terminal states are not pending — by then the reply (or the
    failure) is already on the thread."""
    from domains.runs.models import Run
    from domains.runs.services.active_runs import ACTIVE_RUN_STATUSES

    runs = await Run.nodes.filter(
        surface="comment", status__in=list(ACTIVE_RUN_STATUSES)
    )
    return filter_pending_for_subject(runs, subject_type, subject_uid)


async def trigger_opensweep_reply(
    comment: Comment, subject_type: CommentSubjectType, subject: Any
) -> str:
    """Dispatch the background run answering an @opensweep mention.

    Returns the run uid, or "" when dispatch failed (logged, never raised)."""
    try:
        from domains.runs.services.lifecycle import trigger_run

        thread = await render_thread(subject_type, comment.subject_uid)
        # Scope @-mentions to the org that owns the thread's subject (F2): the
        # subject was already tenancy-checked at write time, so its repo's org
        # is the caller's org. Mentions outside that org's repos are dropped.
        # The thread's own repo is always in scope; resolving the full org set
        # is best-effort so a DB hiccup narrows the scope rather than aborting
        # the reply.
        allowed_repo_uids = {subject.repository_uid}
        try:
            from domains.repositories.models import Repository
            from domains.tenancy import org_repo_uids

            repo = await Repository.nodes.get_or_none(uid=subject.repository_uid)
            if repo is not None:
                allowed_repo_uids = await org_repo_uids(repo.org_uid)
        except Exception:  # noqa: BLE001 — narrow scope, never block the reply
            pass
        mentioned = await render_mentioned_items(
            list(comment.mentions or []), allowed_repo_uids
        )
        # Specialized refine run: the comment-reply template IS the
        # instructions (custom_intent), so a replace overlay never displaces
        # its reply contract; org append guidance still stacks.
        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=subject.repository_uid,
            agent_key="refine",
            stage="refine",
            repo_guidance="",
            custom_intent=build_opensweep_comment_intent(
                subject_type, subject, comment, thread=thread, mentioned=mentioned
            ),
        )
        intent = composed.text
        policy = await ensure_policy_for_effort(Effort.NORMAL)
        target: dict[str, Any] = {
            f"{subject_type.value}_uid": subject.uid,
            "comment_uid": comment.uid,
            # Generic keys so any thread can find its in-flight opensweep runs
            # without knowing the typed key above.
            "subject_type": subject_type.value,
            "subject_uid": subject.uid,
        }
        links = {field: "" for field in _LINK_FIELD.values()}
        if subject_type in _LINK_FIELD:
            links[_LINK_FIELD[subject_type]] = subject.uid
        run = await trigger_run(
            repository_uid=subject.repository_uid,
            intent=intent,
            playbook="refine",
            title=f"@opensweep: {(comment.body or '').strip()[:80]}",
            target=target,
            run_policy_uid=policy.uid,
            trigger=RunTrigger.EVENT,
            triggered_by=comment.author_uid,
            surface="comment",
            **links,
        )
        return run.uid
    except Exception as exc:  # noqa: BLE001 — comment creation must survive dispatch failure
        logger.warning(
            f"@opensweep dispatch failed for comment {comment.uid}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "comments"},
        )
        return ""
