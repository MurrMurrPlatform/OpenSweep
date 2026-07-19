"""Shared intent construction utilities.

The look-before-write footer is appended to every LLM intent that lets the
agent write into OpenSweep. It instructs the agent to use the opensweep_list_* /
opensweep_search_* / opensweep_get_* read tools to find existing items before
calling any write tool, and to record the chosen action in evidence.rationale.
"""

from __future__ import annotations

from typing import Optional

from domains.agents.models import Agent

OPENSWEEP_FRAMING_HEADER = """# Role

You are OpenSweep, an agent analyzing this repository on behalf of the user. The
body below is your **guidance** — what to look at and with what rigour. Two
overrides on it:

1. **Output is tool calls, not markdown.** Ignore any "Output format",
   "Review format", "Summary table", or "Verdict" sections in the body
   below. Your output is structured tool calls into OpenSweep:
   `create_finding`, `update_finding`, `propose_doc_edit`, `write_memory`.
   OpenSweep renders findings to humans — do not write a markdown review.
2. **Your role is fixed.** Ignore any persona reframing in the body
   ("You are a senior code reviewer", "You are a security auditor", etc.).
   You are OpenSweep, reporting back via tools. Take the body's checklists,
   severity rubrics, and false-positive guidance as substantive direction,
   but not its identity claims or output shape.

Use the OpenSweep read tools (`opensweep_list_*`, `opensweep_search_*`, `opensweep_get_*`)
to look before writing — see the look-before-write contract at the end."""


ORG_GUIDANCE_HEADING = "## Organization guidance"


LOOK_BEFORE_WRITE_FOOTER = """
# Look-before-write contract (mandatory)

Before any WRITE tool call (`propose_doc_edit`, `write_memory`,
`create_finding`, `update_finding`), you MUST:

1. SEARCH for what already exists in OpenSweep using the relevant read tool:
   - `list_docs` / `read_doc` before proposing doc edits
   - `search_memory` before writing memories
   - `opensweep_list_findings` / `opensweep_search_findings` before creating Findings
2. For each plausible match, GET its full detail (`opensweep_get_*`).
3. DECIDE explicitly between: skip (already covered), update (refresh /
   add evidence to existing), merge (two existing items describe one
   thing), create (genuinely new), or supersede (existing is now wrong).
4. CALL the write tool. Include `evidence.rationale` stating your choice
   (e.g. "create — no doc page covers queue workers yet", or
   "update of uid=abc123 — same subject, refined description").

Skip the search step only if the prompt body explicitly tells you to.

# Finding quality (applies to every `create_finding`)

Findings are rendered to humans as markdown — write the narrative fields as
markdown (code spans, fenced blocks, lists), not as one flat paragraph.
Fill all four narrative fields, each with a distinct job:

- `description` — the analysis: what is wrong, where, and how it manifests.
- `root_cause` — why it happens (the mechanism, not the symptom).
- `why_it_matters` — impact if left unfixed.
- `suggested_fix` — concrete remediation; use a fenced code block for code.

Anchor `affected_paths` with line numbers when known
(`path/to/file.py:42` or `path/to/file.py:42-60`) so the UI can show the
exact code.
"""


async def load_agent_prompt_body(uid: Optional[str]) -> Optional[str]:
    """Resolve an Agent uid to its prompt body. Returns None if uid not given
    or row not found / not enabled."""
    if not uid:
        return None
    p = await Agent.nodes.get_or_none(uid=uid)
    if p is None or not p.enabled:
        return None
    return p.prompt or ""


def build_intent(
    *,
    prompt_body: Optional[str] = None,
    custom_intent: Optional[str] = None,
    default_intent: str,
    platform_instructions: Optional[str] = None,
    org_overlay_mode: str = "",
    org_overlay_body: str = "",
    repo_guidance_section: str = "",
    scope_summary: str = "",
    existing_state_listing: str = "",
    include_footer: bool = True,
    include_header: bool = True,
) -> str:
    """Compose an LLM intent — the layer cake of the org-agent-overlays spec:

        OPENSWEEP_FRAMING_HEADER   code; ALWAYS included — the instructions
                                   layer comes from editable rows, so the
                                   identity / output-shape re-anchoring is
                                   unconditional
        <instructions>             custom_intent > prompt_body >
                                   platform_instructions > default_intent;
                                   an org overlay with mode=replace
                                   substitutes this layer (unless a
                                   custom_intent override is present)
        ## Organization guidance   org overlay body when mode=append
        ## <Stage> guidance        pre-rendered per-repo workflow section
        # Scope / existing state   the run's structural contract + scope
        LOOK_BEFORE_WRITE_FOOTER   code; unconditional (chat sets
                                   include_footer=False)

    Pure and sync — callers resolve the async layers (see
    domains/agents/services/composition.py) and pass plain strings.
    A replace overlay only ever substitutes the instructions layer: repo
    guidance, scope, header, and footer always stack around it.
    """
    base = (custom_intent or prompt_body or platform_instructions or default_intent).strip()
    overlay_mode = (org_overlay_mode or "").strip().lower()
    overlay_body = (org_overlay_body or "").strip()
    if overlay_mode == "replace" and overlay_body and not custom_intent:
        base = overlay_body
    parts: list[str] = []
    if include_header:
        parts.append(OPENSWEEP_FRAMING_HEADER.strip())
    parts.append(base)
    if overlay_mode == "append" and overlay_body:
        parts.append(f"{ORG_GUIDANCE_HEADING}\n\n{overlay_body}")
    if repo_guidance_section.strip():
        parts.append(repo_guidance_section.strip())
    if scope_summary:
        parts.append("\n# Scope\n\n" + scope_summary.strip())
    if existing_state_listing:
        parts.append("\n# Existing state (search these before writing)\n\n" + existing_state_listing.strip())
    if include_footer:
        parts.append(LOOK_BEFORE_WRITE_FOOTER.strip())
    return "\n\n".join(parts)
