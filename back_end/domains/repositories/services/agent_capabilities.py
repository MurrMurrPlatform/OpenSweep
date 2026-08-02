"""Per-repo agent capability gates.

`agent_autonomy` (Repository.agent_autonomy) is the operator's opt-in that lets
agents perform otherwise human-only operations for a repo through the
platform-tool MCP surface — currently ticket status transitions, including the
Gate-1 approval (backlog → todo). Off by default: an agent must never promote
its own proposed work into implementable work unless the operator allowed it.
"""

from __future__ import annotations

from domains.repositories.models import Repository


async def agent_autonomy_enabled(repository_uid: str) -> bool:
    """True iff the repo has opted into agent autonomy. Unknown repo → False."""
    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    return bool(getattr(repo, "agent_autonomy", False)) if repo else False
