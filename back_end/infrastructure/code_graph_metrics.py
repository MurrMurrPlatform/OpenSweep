"""Deterministic structural metrics from the workspace code graph.

The code graph was agent-facing only: indexed on every clone, exposed over
MCP, and never read by the platform. So every structural judgement in an
architecture sweep — is there an import cycle, which module does everything
touch, which file has accumulated unrelated responsibilities — was an LLM
reading files one context window at a time, when the answer was sitting in a
SQLite index we had already built.

This closes that gap in the shape the codebase already uses for analyzers
(`execution/services/static_analysis.py`): **tools find candidates, the agent
investigates**. A cycle reported here is a fact; whether it matters, and what
to do about it, is still the agent's call.

Design note — extraction here, analysis in Python. The binary's `query_graph`
speaks a Cypher SUBSET: `MATCH`/`RETURN`/aggregate/`ORDER BY`/`LIMIT` work,
but property-to-property `WHERE` (`a.file_path <> b.file_path`) is a parse
error, and there are no path queries. Rather than encode structural analysis
in an undocumented, partially-implemented dialect of an optional external
binary — where a silent parse failure would read as "no problems found" —
we pull the raw edge list and compute over it here, where the logic is
testable and a failure is visible.

Always optional, exactly like `code_graph.py` and the analyzer pass: a
missing binary, an unindexed workspace, a timeout, or a changed output shape
yields no candidates and never fails a run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from logging_config import logger

#: Per-query ceiling. The queries are indexed lookups over a local SQLite
#: cache; anything slower than this means something is wrong, and a global
#: sweep must not wait on it.
QUERY_TIMEOUT_SECONDS = 30

#: How many rows each metric contributes. These land in a prompt, so the cap
#: is about attention, not capacity: twenty coupling hotspots is a list nobody
#: reads, and the tail is exactly where the noise is.
TOP_N = 10

#: A cycle longer than this is almost always an artifact of over-broad import
#: resolution (a package importing its own submodules), not a design problem
#: an agent can act on.
MAX_CYCLE_LEN = 12


async def _cli(*args: str, workspace_path: str) -> dict[str, Any] | None:
    """One `codebase-memory-mcp cli …` call, parsed. None on any failure."""
    from infrastructure.code_graph import _env_for, code_graph_binary

    binary = code_graph_binary()
    if not binary:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "cli",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**_env_for(workspace_path), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=QUERY_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — structural metrics are a bonus
        logger.warning(
            f"code-graph query failed: {type(exc).__name__}: {exc}",
            extra={"tag": "code-graph"},
        )
        return None
    # The binary logs to stdout before the JSON payload; take the last line
    # that parses as an object rather than assuming a clean stream.
    for line in reversed((out or b"").decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


async def project_for_workspace(workspace_path: str) -> str:
    """The indexed project name covering this workspace, or "".

    Resolved by matching `root_path` rather than reconstructing the binary's
    slug rule — that rule is its business, and guessing it would break
    silently the day it changes.
    """
    payload = await _cli("list_projects", workspace_path=workspace_path)
    if not payload:
        return ""
    wanted = (workspace_path or "").rstrip("/")
    for project in payload.get("projects") or []:
        if str(project.get("root_path") or "").rstrip("/") == wanted:
            return str(project.get("name") or "")
    return ""


async def query(project: str, cypher: str, *, workspace_path: str) -> list[list[Any]]:
    """Rows for one query. [] on any failure, including a parse error."""
    if not project:
        return []
    payload = await _cli(
        "query_graph",
        "--project",
        project,
        "--query",
        cypher,
        workspace_path=workspace_path,
    )
    if not payload or "rows" not in payload:
        # A rejected query comes back as an error object; treat it as no data
        # rather than letting a dialect gap read as a clean structural bill.
        if payload and payload.get("error"):
            logger.warning(
                f"code-graph query rejected: {payload.get('error')}",
                extra={"tag": "code-graph"},
            )
        return []
    rows = payload.get("rows")
    return [r for r in rows if isinstance(r, list)] if isinstance(rows, list) else []


# ── Pure analysis ────────────────────────────────────────────────────────


def _clean_edges(rows: list[list[Any]]) -> list[tuple[str, str]]:
    """(src, dst) file pairs, self-edges and blanks dropped."""
    out: list[tuple[str, str]] = []
    for row in rows:
        if len(row) < 2:
            continue
        src, dst = str(row[0] or "").strip(), str(row[1] or "").strip()
        if src and dst and src != dst:
            out.append((src, dst))
    return out


def import_cycles(edges: list[tuple[str, str]], *, max_len: int = MAX_CYCLE_LEN) -> list[list[str]]:
    """Strongly connected components of the file-level import graph. Pure.

    An SCC with more than one file IS a cycle: every file in it can reach
    every other. Reported smallest-first — a two-file cycle is a concrete,
    fixable statement, while a fifteen-file one is usually a symptom of the
    indexer resolving a package to its own submodules.

    Iterative Tarjan: a deep import chain in a large repo would blow the
    recursion limit, and this must never be the thing that fails a run.
    """
    graph: dict[str, set[str]] = {}
    for src, dst in edges:
        graph.setdefault(src, set()).add(dst)
        graph.setdefault(dst, set())

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(graph[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(graph[nxt])))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))

    return sorted(
        (c for c in components if len(c) <= max_len), key=lambda c: (len(c), c)
    )


def coupling_hotspots(edges: list[tuple[str, str]], *, top: int = TOP_N) -> list[tuple[str, int]]:
    """Files the most OTHER files import, by distinct dependents. Pure.

    Distinct dependents, not raw edge count: one view importing the same UI
    component five times is one coupling relationship, not five.
    """
    dependents: dict[str, set[str]] = {}
    for src, dst in edges:
        dependents.setdefault(dst, set()).add(src)
    ranked = sorted(dependents.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(path, len(srcs)) for path, srcs in ranked[:top]]


def god_modules(rows: list[list[Any]], *, top: int = TOP_N) -> list[tuple[str, int]]:
    """Files defining the most symbols. Pure — rows are (file_path, count)."""
    out: list[tuple[str, int]] = []
    for row in rows:
        if len(row) < 2:
            continue
        path = str(row[0] or "").strip()
        try:
            count = int(row[1])
        except (TypeError, ValueError):
            continue
        if path:
            out.append((path, count))
    return sorted(out, key=lambda kv: (-kv[1], kv[0]))[:top]


def render_section(
    cycles: list[list[str]],
    hotspots: list[tuple[str, int]],
    gods: list[tuple[str, int]],
) -> str:
    """The prompt block. "" when there is nothing to say. Pure."""
    if not (cycles or hotspots or gods):
        return ""
    parts = [
        "## Structural candidates (from the code graph)",
        "Derived from the workspace's import/definition graph — these are "
        "FACTS about shape, not findings. Investigate each and file only what "
        "carries a real cost; a hub module with many dependents is often "
        "exactly right.",
    ]
    if cycles:
        lines = "\n".join(
            f"- {' → '.join(c)} → {c[0]}" for c in cycles[:TOP_N]
        )
        parts.append(f"### Import cycles ({len(cycles)})\n{lines}")
    if hotspots:
        lines = "\n".join(f"- {p} — imported by {n} file(s)" for p, n in hotspots)
        parts.append(f"### Most-depended-on files\n{lines}")
    if gods:
        lines = "\n".join(f"- {p} — defines {n} symbols" for p, n in gods)
        parts.append(f"### Largest definition counts\n{lines}")
    return "\n\n".join(parts)


# ── Entry point ──────────────────────────────────────────────────────────

_IMPORT_EDGES = "MATCH (a)-[:IMPORTS]->(b) RETURN a.file_path AS src, b.file_path AS dst"
_DEFINE_COUNTS = (
    "MATCH (f)-[:DEFINES]->(s) RETURN f.file_path AS f, count(*) AS n "
    "ORDER BY n DESC LIMIT 50"
)


async def structural_candidates(workspace_path: str) -> str:
    """The rendered structural section for a whole-repo sweep. "" if absent.

    Best-effort by construction — see the module docstring. Callers append
    this to a run's context the way `_maybe_run_static_analysis` appends
    analyzer candidates.
    """
    if not workspace_path:
        return ""
    try:
        project = await project_for_workspace(workspace_path)
        if not project:
            return ""
        edges = _clean_edges(await query(project, _IMPORT_EDGES, workspace_path=workspace_path))
        defines = await query(project, _DEFINE_COUNTS, workspace_path=workspace_path)
        return render_section(
            import_cycles(edges), coupling_hotspots(edges), god_modules(defines)
        )
    except Exception as exc:  # noqa: BLE001 — never turn-fatal
        logger.warning(
            f"structural candidates unavailable for {workspace_path!r}: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "code-graph"},
        )
        return ""
