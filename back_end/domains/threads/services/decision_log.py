"""Distill a planning conversation into a decision log for carry-over.

Deterministic: user messages are the decisions (kept verbatim, oldest first);
the final assistant_text is the agent's closing summary. When over budget,
drop the OLDEST user messages first — recent decisions supersede old ones.
"""

from __future__ import annotations

HEADER = "## Decisions from the planning conversation\n"


def build_decision_log(events: list[dict], max_chars: int = 8000) -> str:
    user_msgs = [
        (e.get("text") or "").strip()
        for e in events
        if e.get("type") == "user_message" and (e.get("text") or "").strip()
    ]
    assistant_texts = [
        (e.get("text") or "").strip()
        for e in events
        if e.get("type") == "assistant_text" and (e.get("text") or "").strip()
    ]
    if not user_msgs and not assistant_texts:
        return ""

    blocks: list[str] = [f"- (user) {m}" for m in user_msgs]
    if assistant_texts:
        blocks.append(f"- (agent, closing summary) {assistant_texts[-1]}")

    # Budget: keep the tail (most recent), drop oldest blocks first.
    kept: list[str] = []
    total = 0
    for block in reversed(blocks):
        if total + len(block) + 1 > max_chars:
            break
        kept.append(block)
        total += len(block) + 1
    return HEADER + "\n".join(reversed(kept))
