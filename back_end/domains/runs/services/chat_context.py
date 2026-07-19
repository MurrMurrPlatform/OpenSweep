"""First-turn context for opensweep chat-bubble runs (surface="chat").

The widget passes {subject_type, subject_uid} for whatever the user was
looking at when the chat started; we snapshot it into a preamble so the
agent knows what "this ticket" means. Best-effort: an unknown or deleted
subject yields no preamble, never an error — the chat still works.
"""

from __future__ import annotations

from logging_config import logger

_CHAT_CONTRACT = (
    "You are OpenSweep, the platform agent, talking to a maintainer through the "
    "platform chat widget. You have the opensweep_platform_* tools — use them to "
    "look up and change platform data when asked (tickets, findings, docs, "
    "memories, news). The repository clone is read-only: never modify code. "
    "Answer conversationally in the chat itself; do not use "
    "opensweep_platform_add_comment to reply here."
)


async def build_chat_preamble(context: dict[str, str] | None, *, org_uid: str = "") -> str:
    """The system-ish text prepended to a chat run's first turn.

    Chat uses the org-agent-overlays mechanism with a thinner structural
    wrapper: the code-owned contract above, then the platform chat
    instructions with the org's overlay applied — no framing header and no
    look-before-write footer. Layer failures degrade silently (best-effort,
    like the context snapshot)."""
    parts = [_CHAT_CONTRACT]
    try:
        from domains.agents.services.composition import chat_instruction_layers

        layers = await chat_instruction_layers(org_uid)
        if layers:
            parts.append(layers)
    except Exception as exc:  # noqa: BLE001 — guidance is a layer, never a blocker
        logger.warning(
            f"chat instruction layers failed for org {org_uid}: {exc}",
            extra={"tag": "runs"},
        )
    snapshot = await _context_snapshot(context)
    if snapshot:
        parts.append(
            "The maintainer started this chat while viewing the following "
            "item — 'this' likely refers to it:\n" + snapshot
        )
    return "\n\n".join(parts)


async def _context_snapshot(context: dict[str, str] | None) -> str:
    subject_type_raw = (context or {}).get("subject_type", "").strip()
    subject_uid = (context or {}).get("subject_uid", "").strip()
    if not subject_type_raw or not subject_uid:
        return ""
    try:
        from domains.comments.schemas import CommentSubjectType
        from domains.comments.subjects import get_subject, subject_snapshot

        subject_type = CommentSubjectType(subject_type_raw)
        subject = await get_subject(subject_type, subject_uid)
        if subject is None:
            return ""
        return subject_snapshot(subject_type, subject)
    except Exception as exc:  # noqa: BLE001 — context is a nicety, never a blocker
        logger.warning(
            f"chat context snapshot failed for {subject_type_raw}:{subject_uid}: {exc}",
            extra={"tag": "runs"},
        )
        return ""
