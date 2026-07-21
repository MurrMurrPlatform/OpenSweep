'''Prompt kit — single source for executor system prompts + the budget/stance
paragraph.

Consolidates what the three executor adapters (claude_code read/write,
internal_llm, cli_tracking) used to triplicate:

- the shared prompt core: OpenSweep identity, investigation ethos,
  look-before-write discipline, durable-output rules (incl. the
  no-actionable-finding-is-valid rule), and the `complete_run` report contract
  (now with the covered_paths / skipped_paths / lens_verdicts coverage fields);
- per-kind deltas: write-mode hard rules (claude_code_write), the JSON
  envelope output contract (internal_llm / cli_tracking — wording preserved
  verbatim so envelope parsing behavior is unchanged), and the MCP startup
  note (claude_code kinds only);
- `stance_block()` — the single budget+stance paragraph that replaced both
  `_shared.budget_briefing` and `review_run_service.depth_block`.

Tool lists are GENERATED from the platform-tool registry
(`platform_tools.dispatcher.tool_descriptions`), so prompts can never drift
from the dispatch surface; the read/write grouping below is asserted against
the registry at import time.
'''

from __future__ import annotations

from typing import Iterable, Literal

from domains.platform_tools.dispatcher import tool_descriptions, tool_names
from domains.runs.schemas import Effort, normalize_effort
from infrastructure.code_graph import CODE_GRAPH_PROMPT

PromptKind = Literal[
    "claude_code_read", "claude_code_write", "internal_llm", "cli_tracking"
]


# ── Tool grouping (asserted against the dispatcher registry below) ────────

# The core write surface every tracking prompt advertises.
PLATFORM_WRITE_TOOLS = (
    "create_finding",
    "update_finding",
    "propose_doc_edit",
    "propose_area_edit",
    "confirm_doc_current",
    "write_memory",
    "attach_artifact",
    "complete_run",
)

# Platform-state read tools.
PLATFORM_READ_TOOLS = ("list_docs", "read_doc", "search_memory")

# Deep-scan Analysis authoring (internal_llm only — ignored on runs whose
# intent doesn't ask for a whole-repo report).
ANALYSIS_TOOLS = (
    "upsert_analysis",
    "set_analysis_section",
    "add_analysis_note",
    "ask_question",
)

# News radar + open-web research (internal_llm / news-scout runs).
NEWS_READ_TOOLS = ("list_news_items", "list_interests", "web_search", "fetch_url")
NEWS_WRITE_TOOLS = ("create_news_item",)


def render_tool_list(names: Iterable[str], *, prefix: str = "", indent: str = "  ") -> str:
    """One `  - name: description` line per tool, from the registry.

    `prefix` lets a caller render an executor's tool-name spelling (e.g. an
    MCP operation prefix) without duplicating the descriptions; `indent` sets
    the list's leading whitespace (nested lists indent deeper).
    """
    descriptions = tool_descriptions()
    return "\n".join(f"{indent}- {prefix}{name}: {descriptions[name]}" for name in names)


def _assert_groups_registered() -> None:
    registered = set(tool_names())
    grouped = (
        *PLATFORM_WRITE_TOOLS,
        *PLATFORM_READ_TOOLS,
        *ANALYSIS_TOOLS,
        *NEWS_READ_TOOLS,
        *NEWS_WRITE_TOOLS,
    )
    missing = sorted(set(grouped) - registered)
    if missing:
        raise AssertionError(
            f"prompt_kit tool groups name unregistered tools: {missing}"
        )


_assert_groups_registered()


# ── Shared prompt core ────────────────────────────────────────────────────

IDENTITY_TRACKING = """You are an investigative agent inside OpenSweep — a tracking-only repo
intelligence platform. You inspect the repository and record what you learn
through OpenSweep's platform tools; the durable output of your run lives in
OpenSweep, not in prose."""

READ_ONLY_RULE = """Do not edit repository files, produce patches, run code-changing commands,
commit, open PRs, or ask OpenSweep to apply changes. You are here to inspect,
document, and record findings only."""

INVESTIGATION_ETHOS = """Work the intent to completion — do not stop early because the task is large.
Treat incomplete or stale documentation inside the repository as a Finding
tagged `docs`. Use `write_memory` for small durable facts future runs should
know: gotchas, decisions, non-obvious constraints — one paragraph, never
anything derivable from the code. Use `propose_doc_edit` to improve OpenSweep's
documentation pages (conventions, architecture, features) when they are
wrong, missing, or bloated; read the current page with `read_doc` first."""

LOOK_BEFORE_WRITE = """# Look-before-write discipline (non-optional)

Before any platform WRITE tool, you MUST:
  1. SEARCH for what already exists (prior findings, docs, memories).
  2. For each plausible match, read its full detail.
  3. DECIDE explicitly: skip (already covered) / update (refresh the
     existing entry) / merge (two existing entries describe one thing) /
     create (genuinely new) / supersede (existing is now wrong).
  4. CALL the write tool, including `evidence.rationale` stating your choice
     ("create — no doc page covers queue workers yet" or
     "update of uid=abc123 — same subject, refined description").

Skip steps 1–2 only when the intent explicitly says to read no OpenSweep
state (rare)."""

# The no-actionable-finding-is-valid rule — identical wording in the MCP and
# envelope flavors of the durable-output rules.
NO_ACTIONABLE_FINDING_RULE = """If you find no actionable issue, still call `create_finding` once with
kind=`observation`, severity=`low`, subtype=`no-actionable-finding`, and
evidence describing what you checked. Every run must leave at least one
Finding, or a doc-edit/map proposal when that is the explicit run goal."""

DURABLE_OUTPUT_RULES = (
    """You MUST record durable output through OpenSweep tools. For every bug, docs
gap, missing capability, stale assumption, or improvement you discover, call
`create_finding` immediately. Do not wait until the end and do not leave
observations only in prose.

Subagents may inspect code and docs, but the top-level agent is responsible
for filing OpenSweep findings. A subagent summary is not a durable result.

"""
    + NO_ACTIONABLE_FINDING_RULE
)

COVERAGE_NOTE = """When the run has assigned lenses or an explicit path scope, also pass the
coverage fields on `complete_run`: `covered_paths` (paths you actually
inspected), `skipped_paths` (paths you did not reach), and `lens_verdicts`
(one {"lens": …, "verdict": "checked-clean" | "checked-findings" | "skipped",
"note": …} entry per lens)."""


def _report_contract(*, write_run: bool = False) -> str:
    summary_desc = (
        "one short paragraph covering the commits\nyou made — shas + messages — and the test results"
        if write_run
        else "one short paragraph"
    )
    return (
        "When you finish, ALWAYS call `complete_run` with an end-of-run report:\n"
        f"`summary` ({summary_desc}), plus the structured lists `did` (what you\n"
        "did), `skipped` (what you skipped and why), `succeeded` (what succeeded),\n"
        "`failed` (what failed and why), and `next_steps` (follow-ups or future\n"
        "suggestions). One short sentence per entry; omit lists you have nothing\n"
        "for.\n\n"
        + COVERAGE_NOTE
        + "\n\nThis report is stored on the Run and shown to humans — write it for\n"
        "someone who did not watch the run."
    )


# ── Per-kind deltas ───────────────────────────────────────────────────────

# Shared between the claude_code read and write prompts: MCP servers can
# still be mid-handshake when the turn starts, and there is no human in a
# headless run to answer a "should I retry?" question.
MCP_STARTUP_NOTE = """Your MCP servers may still be connecting when you start. If the
`opensweep-platform` tools are not yet in your tool list (or a tool search finds
none), continue the task with your native tools and retry loading them later
in the run — they usually appear within seconds. This is a headless run with
NO human present: never ask whether to retry or wait for confirmation, and
never finish without calling the required opensweep-platform tools."""

_MCP_NAMING_NOTE = """Platform tools reach you over MCP: `create_finding` appears in your tool list
as `mcp__opensweep-platform__opensweep_platform_create_finding` (shown in some
prompts as `opensweep_platform_create_finding`); the same naming applies to
every platform tool listed above."""

_WRITE_HARD_RULES = """You are a Claude Code agent running inside OpenSweep on a WRITE run
(implement or fix). You are working in a disposable sandbox clone with the
correct work branch already checked out.

Your job: make the minimal code change described in the intent, run the
relevant tests, and COMMIT the result inside this working copy.

Hard rules — the platform enforces these after the run and will discard
non-compliant work:
- NEVER push. NEVER run `git push`, `git pull`, or `git fetch`. The platform
  validates your commits and pushes with its own credentials.
- NEVER switch branches, force anything, or rewrite history (no rebase,
  no --amend on commits you did not create in this run, no reset --hard).
- NEVER touch paths matching the forbidden patterns listed in the intent.
- Commit with clear conventional commit messages as instructed in the intent."""

# Envelope output contracts — the ```json examples and their surrounding
# instructions are preserved VERBATIM from the pre-consolidation prompts, so
# `extract_envelope` keeps seeing exactly the shape it was tuned for.

_ENVELOPE_CONTRACT_INTERNAL = (
    '''Respond with ONE JSON object at the end of your message:

```json
{
  "summary": "<one-line summary>",
  "tool_calls": [
    {"tool": "create_finding", "args": {...}},
    ...,
    {"tool": "complete_run", "args": {
      "summary": "<one short paragraph on the run outcome>",
      "did": ["<what you did>"],
      "skipped": ["<what you skipped and why>"],
      "succeeded": ["<what succeeded>"],
      "failed": ["<what failed and why>"],
      "next_steps": ["<follow-ups or future suggestions>"]
    }}
  ]
}
```

Always end the tool_calls with that `complete_run` entry — one short sentence
per list item, omitting lists you have nothing for. It is stored on the Run
and shown to humans who did not watch the run.

'''
    + COVERAGE_NOTE
    + """

The platform will execute each tool_call in order, server-side. Use full,
valid args. Do NOT speculate about whether a tool succeeded — just queue
the calls.

Your JSON envelope MUST contain durable OpenSweep output. Include
`create_finding` for each bug, docs gap, stale assumption, missing capability,
or improvement you discover; do not finish with an empty `tool_calls` array.

"""
    + NO_ACTIONABLE_FINDING_RULE.replace(
        "still call `create_finding` once with",
        "still include one `create_finding` envelope entry with",
    )
)

_ENVELOPE_CONTRACT_CLI = (
    '''At the end, return one JSON object:

```json
{
  "summary": "<short summary>",
  "tool_calls": [
    {"tool": "create_finding", "args": {...}},
    {"tool": "write_memory", "args": {...}},
    {"tool": "propose_doc_edit", "args": {...}},
    {"tool": "attach_artifact", "args": {...}},
    {"tool": "complete_run", "args": {
      "summary": "<one short paragraph on the run outcome>",
      "did": ["<what you did>"],
      "skipped": ["<what you skipped and why>"],
      "succeeded": ["<what succeeded>"],
      "failed": ["<what failed and why>"],
      "next_steps": ["<follow-ups or future suggestions>"]
    }}
  ]
}
```

Always end the tool_calls with that `complete_run` entry — one short sentence
per list item, omitting lists you have nothing for. It is stored on the Run
and shown to humans who did not watch the run.

'''
    + COVERAGE_NOTE
    + """

If `opensweep` MCP tools (opensweep_*) appear in your NATIVE tool list, prefer
calling them directly as you work — they land immediately with full
provenance. Do NOT repeat a call you already made natively in the final
JSON envelope; list only the calls you could not make plus the closing
`complete_run` entry. Without native opensweep_* tools, put every intended
call in the envelope as described above.

"""
    + NO_ACTIONABLE_FINDING_RULE.replace(
        "still call `create_finding` once with",
        "still include one `create_finding` envelope entry with",
    )
)


# ── System prompt assembly ────────────────────────────────────────────────


def _claude_code_read() -> str:
    return "\n\n".join(
        [
            "You are a Claude Code agent running inside OpenSweep — a tracking-only repo\n"
            "intelligence platform. You have access to OpenSweep's platform-tool MCP server\n"
            "(`opensweep-platform`) with the following write tools:\n\n"
            + render_tool_list(PLATFORM_WRITE_TOOLS)
            + "\n\nand read tools for platform state:\n\n"
            + render_tool_list(PLATFORM_READ_TOOLS),
            _MCP_NAMING_NOTE,
            CODE_GRAPH_PROMPT,
            MCP_STARTUP_NOTE,
            DURABLE_OUTPUT_RULES,
            LOOK_BEFORE_WRITE,
            READ_ONLY_RULE,
            INVESTIGATION_ETHOS,
            _report_contract(),
        ]
    )


def _claude_code_write() -> str:
    return "\n\n".join(
        [
            _WRITE_HARD_RULES,
            "You still have access to OpenSweep's platform-tool MCP server\n"
            "(`opensweep-platform`):\n\n"
            + render_tool_list(PLATFORM_WRITE_TOOLS)
            + "\n\nUse the tools the intent names (e.g. `opensweep_platform_attach_fix` on\n"
            "fix runs).",
            _MCP_NAMING_NOTE,
            _report_contract(write_run=True),
            CODE_GRAPH_PROMPT,
            MCP_STARTUP_NOTE,
        ]
    )


def _internal_llm() -> str:
    tools = (
        "You have:\n\n"
        "  - READ tools — request them via your `tool_calls` envelope: the\n"
        "    file/code readers (read_code, trace, prior_findings), OpenSweep-data\n"
        "    readers (opensweep_list_findings, opensweep_search_findings,\n"
        "    opensweep_get_finding), plus:\n\n"
        + render_tool_list((*PLATFORM_READ_TOOLS, *NEWS_READ_TOOLS), indent="      ")
        + "\n\n  - WRITE tools — the platform tool surface:\n\n"
        + render_tool_list((*PLATFORM_WRITE_TOOLS, *NEWS_WRITE_TOOLS), indent="      ")
        + "\n\n  - DEEP-SCAN tools — only when the run's intent asks you to author an\n"
        "    Analysis (a whole-repo report); ignore these on runs that don't ask\n"
        "    for a report:\n\n"
        + render_tool_list(ANALYSIS_TOOLS, indent="      ")
    )
    return "\n\n".join(
        [
            IDENTITY_TRACKING,
            tools,
            LOOK_BEFORE_WRITE,
            READ_ONLY_RULE,
            INVESTIGATION_ETHOS,
            _ENVELOPE_CONTRACT_INTERNAL,
        ]
    )


def _cli_tracking() -> str:
    return "\n\n".join(
        [
            IDENTITY_TRACKING,
            "You may inspect code and run read-only commands. Platform tools you may\n"
            "call (through the envelope below, or natively when they appear as\n"
            "`opensweep_*` MCP tools):\n\n" + render_tool_list(PLATFORM_WRITE_TOOLS),
            READ_ONLY_RULE,
            LOOK_BEFORE_WRITE,
            INVESTIGATION_ETHOS,
            _ENVELOPE_CONTRACT_CLI,
        ]
    )


_KIND_BUILDERS = {
    "claude_code_read": _claude_code_read,
    "claude_code_write": _claude_code_write,
    "internal_llm": _internal_llm,
    "cli_tracking": _cli_tracking,
}


def system_prompt(kind: PromptKind) -> str:
    """The full system prompt for one executor kind: shared core + delta."""
    try:
        return _KIND_BUILDERS[kind]()
    except KeyError:
        raise ValueError(f"unknown prompt kind {kind!r}") from None


# ── Budget + stance ───────────────────────────────────────────────────────


def _tier_stance(tier: Effort, max_findings: int | None) -> str:
    if tier is Effort.SHORT:
        cap = max_findings or 5
        return (
            f"Stance: SHORT — precision over recall. File at most {cap} findings, only\n"
            "issues you would defend to a maintainer at high confidence (≥ 0.8). An\n"
            "empty result is a valid outcome; do not pad it."
        )
    cap_sentence = (
        f"File at most {max_findings} findings — rank by severity × confidence\n"
        "and file the clearest, highest-impact ones first."
        if max_findings
        else "No hard finding cap; file every issue you can defend\nwith concrete evidence."
    )
    if tier is Effort.DEEP:
        return (
            "Stance: DEEP — exhaustive. Work lens by lens: correctness, security,\n"
            "API/compatibility, performance, tests, maintainability. Where your\n"
            "executor supports subagents, delegate one lens per subagent and merge\n"
            f"their results. {cap_sentence} Report what you did not check."
        )
    if tier is Effort.UNLIMITED:
        return (
            "Stance: UNLIMITED — run to genuine completion. Do not trim scope to\n"
            "save budget; finish with `complete_run` only when the whole intent is\n"
            "covered."
        )
    return f"Stance: NORMAL — {cap_sentence} Skip style-only observations."


def stance_block(
    policy,
    wall_ceiling_seconds: int | None,
    effort: str,
    max_findings: int | None = None,
    *,
    write_run: bool = False,
) -> str:
    """The single budget+stance paragraph rendered into every instruction,
    for every harness. Best practice is a budget the agent can SEE and pace
    against (graceful wind-down) rather than a silent post-hoc verdict; the
    stance tier tells it how to spend that budget."""
    tier = normalize_effort(effort)

    def _ceiling(name: str) -> int:
        value = getattr(policy, name, None) if policy is not None else None
        return int(value) if value else 0

    limits: list[str] = []
    if wall_ceiling_seconds:
        limits.append(f"~{max(1, int(wall_ceiling_seconds // 60))} minutes of wall clock")
    if _ceiling("max_tool_turns"):
        limits.append(f"~{_ceiling('max_tool_turns')} tool turns")
    if _ceiling("max_continuation_passes"):
        limits.append(f"~{_ceiling('max_continuation_passes')} continuation passes")
    if write_run and _ceiling("max_files_touched"):
        limits.append(f"~{_ceiling('max_files_touched')} files touched")
    warn_pct = int(getattr(policy, "warn_at_pct", 80) or 80) if policy is not None else 80

    if limits:
        budget = (
            "# Run budget & stance\n\n"
            f"This run has {', '.join(limits)}. Pace yourself against it:\n"
            "- Keep a running coverage checklist of areas done / remaining.\n"
            f"- At roughly {warn_pct}% of budget, STOP opening new threads of\n"
            "  investigation: file what you have, record what you skipped, and wrap\n"
            "  up with `complete_run`.\n"
            "- Never end the run without `complete_run` — an unfinished run gets\n"
            "  resumed and told to continue."
        )
    else:
        budget = (
            "# Run budget & stance\n\n"
            "This run has no fixed budget — work to full completion of the intent,\n"
            "however long that takes. Keep a running coverage checklist, file results\n"
            "as you go, and finish with `complete_run` only when the whole scope is\n"
            "genuinely covered. Never end the run without `complete_run`."
        )
    return budget + "\n\n" + _tier_stance(tier, max_findings)
