"""System-agent registry: keys, produces↔playbook mapping, stage mapping.

The single place that translates between the user-facing Agent vocabulary
(`produces`) and the internal run machinery (`playbook`, workflow stage).

System agents are identified by a stable `source_url`; `agent_key` derives
the short key exposed to the UI and used throughout dispatch:

    opensweep://agent/<key>        → <key>            (playbook bases)
    opensweep://workflow/<stage>   → <stage>-guidance (stage guidance defaults)
    opensweep://library/<slug>     → <slug>           (strategy variants)
"""

from __future__ import annotations

from domains.agents.models import Agent

AGENT_URL_PREFIX = "opensweep://agent/"
WORKFLOW_URL_PREFIX = "opensweep://workflow/"
LIBRARY_URL_PREFIX = "opensweep://library/"


def agent_source_url(key: str) -> str:
    return f"{AGENT_URL_PREFIX}{key}"


def workflow_source_url(stage: str) -> str:
    return f"{WORKFLOW_URL_PREFIX}{stage}"


def variant_source_url(slug: str) -> str:
    return f"{LIBRARY_URL_PREFIX}{slug}"


def agent_key(source_url: str) -> str:
    """Stable short key for a system agent; "" for user/imported rows."""
    url = source_url or ""
    if url.startswith(AGENT_URL_PREFIX):
        return url[len(AGENT_URL_PREFIX) :]
    if url.startswith(WORKFLOW_URL_PREFIX):
        return url[len(WORKFLOW_URL_PREFIX) :] + "-guidance"
    if url.startswith(LIBRARY_URL_PREFIX):
        return url[len(LIBRARY_URL_PREFIX) :]
    return ""


def source_url_for_key(key: str) -> str:
    """Inverse of agent_key for system rows."""
    k = (key or "").strip()
    if not k:
        return ""
    if k.endswith("-guidance"):
        return workflow_source_url(k[: -len("-guidance")])
    return agent_source_url(k)


# produces → the internal playbook its runs execute under. code-changes maps
# to implement for standalone dispatch; fix/thread runs are dispatched by the
# delivery/thread services with their playbook set explicitly.
PRODUCES_TO_PLAYBOOK: dict[str, str] = {
    "findings": "ask",
    "answer": "chat",
    "documentation": "document",
    "doc-tree": "ask",
    "analysis": "ask",
    "review-verdict": "review",
    "verification": "verify",
    "code-changes": "implement",
}

# produces a user-created agent may declare. code-changes additionally
# requires the maintainer role (checked in the API layer) and is refused by
# scheduled dispatch outright (write runs need a prepared write sandbox and
# ticket/PR context — only the delivery/thread services dispatch those).
USER_CREATABLE_PRODUCES = {"findings", "answer", "documentation"}
WRITE_PRODUCES = {"code-changes"}


def playbook_for_produces(produces: str) -> str:
    return PRODUCES_TO_PLAYBOOK.get((produces or "").strip(), "ask")


# The overridable system-agent keys, in deterministic listing order — the
# former AGENT_PLAYBOOKS plus audit-stale. deep-scan, generate-docs, and
# map-areas run under the "ask" playbook but carry their own instruction
# bases.
AGENT_KEYS = (
    "chat",
    "ask",
    "review",
    "fix",
    "implement",
    "verify",
    "document",
    "refine",
    "thread",
    "deep-scan",
    "generate-docs",
    "generate-specs",
    "revise-spec",
    "audit-stale",
    "run-campaign",
    "map-areas",
)


def stage_for_agent_key(key: str, playbook: str) -> str:
    """Which workflow stage governs a run, for per-stage run overrides.

    System agent keys carry the sharper signal (generate-docs vs a plain
    ask); direct runs only have their playbook. Returns "" for runs no
    stage governs (chat)."""
    from domains.repositories.services.workflow import STAGES

    k = (key or "").strip()
    if k in {"generate-docs", "generate-specs", "revise-spec", "map-areas"}:
        return "discover"
    if k == "deep-scan":
        return "analysis"
    if k in {"audit-stale", "run-campaign"}:
        return "ask"
    if k in STAGES:
        return k
    pb = (playbook or "").strip()
    return pb if pb in STAGES else ""


async def system_agent_by_url(source_url: str) -> Agent | None:
    """The seeded (possibly admin-edited) system row — enabled or not;
    None when it was deleted."""
    for a in await Agent.nodes.filter(provenance="system", source_url=source_url):
        return a
    return None


async def system_agent_by_key(key: str) -> Agent | None:
    url = source_url_for_key(key)
    if not url:
        return None
    row = await system_agent_by_url(url)
    if row is None:
        # Library variants (opensweep://library/<slug>) share the bare key with
        # the agent/ namespace, but source_url_for_key can only produce the
        # agent/ form. Fall back to the library URL so a variant dispatched
        # directly by key (e.g. a scheduled deep-issue-hunt) resolves its body
        # instead of degrading to the "Run the <key> agent." fallback.
        lib_url = variant_source_url(key)
        if lib_url != url:
            row = await system_agent_by_url(lib_url)
    return row


async def agent_body_by_key(key: str) -> str | None:
    """The ENABLED system body for a key; None when the row was deleted or
    disabled — callers fall back to the in-code copy (`agent_base_fallback`)
    or skip the layer."""
    row = await system_agent_by_key(key)
    if row is None or not row.enabled:
        return None
    return row.prompt or ""
