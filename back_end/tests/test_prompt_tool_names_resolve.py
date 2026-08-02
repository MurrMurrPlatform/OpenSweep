"""Every tool name a shipped prompt mentions must actually resolve.

`test_tool_registry` already checks the prompt-kit tool GROUPS, but the
groups are only what `render_tool_list` enumerates. The contract prose is
written by hand, and it drifted: the look-before-write contract named
`opensweep_list_findings` / `opensweep_search_findings` / `opensweep_get_*`
as MANDATORY before every `create_finding`, while the real MCP operations are
`opensweep_platform_read_list_findings` and friends — which were themselves
absent from the dispatcher registry, so no generated tool list ever mentioned
them either. The result was a mandatory step an agent could not perform, and
audit runs blind to findings earlier runs had already filed.

Nothing caught it because no test reads the prompt TEXT. This one does.
"""

import re

import pytest

from domains.executors import prompt_kit
from domains.platform_tools.dispatcher import tool_names
from domains.runs.services import _intent_helpers as intent_helpers
from mcp_app import OPENSWEEP_PLATFORM_TOOL_OPERATIONS

#: Backticked snake_case tokens that are deliberately NOT tool names — finding
#: fields, enum values, and prose. Kept explicit so a genuinely new tool
#: reference cannot hide behind a broad regex.
_NOT_TOOLS = {
    # create_finding / update_finding argument and evidence field names
    "affected_paths",
    "description",
    "root_cause",
    "suggested_fix",
    "why_it_matters",
    "source_run_uid",
    # complete_run argument names
    "summary",
    "did",
    "skipped",
    "succeeded",
    "failed",
    "next_steps",
    "covered_paths",
    "skipped_paths",
    "lens_verdicts",
    "max_findings",
    # Finding tag values
    "docs",
    # vocabulary values
    "low",
    "medium",
    "high",
    "critical",
    "observation",
    "defect",
    "improvement",
    "gap",
    # prose
    "read",
    "write",
    "opensweep",  # the MCP server name, not a tool
}

_TOKEN = re.compile(r"`([a-z][a-z0-9_]*)`")


def _prompt_sources() -> dict[str, str]:
    """Every shipped prompt string that can name a tool."""
    sources = {
        "OPENSWEEP_FRAMING_HEADER": intent_helpers.OPENSWEEP_FRAMING_HEADER,
        "LOOK_BEFORE_WRITE_FOOTER": intent_helpers.LOOK_BEFORE_WRITE_FOOTER,
    }
    for name in dir(prompt_kit):
        if name.startswith("__"):
            continue
        value = getattr(prompt_kit, name)
        if isinstance(value, str) and "`" in value:
            sources[f"prompt_kit.{name}"] = value
    for kind in prompt_kit._KIND_BUILDERS:
        sources[f"system_prompt({kind!r})"] = prompt_kit.system_prompt(kind)
    return sources


@pytest.mark.parametrize("source", sorted(_prompt_sources()))
def test_every_tool_name_a_prompt_mentions_resolves(source):
    text = _prompt_sources()[source]
    registered = set(tool_names())
    mcp_operations = set(OPENSWEEP_PLATFORM_TOOL_OPERATIONS)

    for token in _TOKEN.findall(text):
        if token in _NOT_TOOLS or token in registered:
            continue
        if token.startswith("opensweep"):
            # A fully-qualified spelling must be a real MCP operation id —
            # this is the exact shape that drifted.
            assert token in mcp_operations, (
                f"{source} names `{token}`, which is not a served MCP "
                f"operation. Real read operations carry a `read` segment, "
                f"e.g. opensweep_platform_read_list_findings."
            )
            continue
        pytest.fail(
            f"{source} names `{token}`, which is neither a registered "
            f"platform tool nor a known non-tool term. Register it in "
            f"platform_tools.dispatcher._TOOLS, fix the spelling, or add it "
            f"to _NOT_TOOLS in this test if it is prose."
        )


def test_the_look_before_write_contract_names_the_findings_read_tools():
    """The regression, pinned directly: the mandatory pre-create_finding step
    must name tools that exist and are advertised to the agent."""
    footer = intent_helpers.LOOK_BEFORE_WRITE_FOOTER
    registered = set(tool_names())

    for tool in ("list_findings", "search_findings", "get_finding"):
        assert f"`{tool}`" in footer, f"contract no longer names {tool}"
        assert tool in registered, f"{tool} is not dispatchable"
        assert tool in prompt_kit.PLATFORM_READ_TOOLS, (
            f"{tool} is dispatchable but never advertised — render_tool_list "
            f"only enumerates the prompt_kit groups"
        )


def test_the_mcp_naming_note_covers_every_advertised_read_tool():
    """The note explains the `read` infix by enumeration, so a read tool it
    omits is one the agent will look up under the wrong name."""
    note = prompt_kit._MCP_NAMING_NOTE
    for tool in prompt_kit.PLATFORM_READ_TOOLS:
        assert tool in note, (
            f"{tool} is advertised as a read tool but _MCP_NAMING_NOTE does "
            f"not mention it, so its MCP spelling is unguessable"
        )
