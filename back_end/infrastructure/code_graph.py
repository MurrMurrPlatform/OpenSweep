"""codebase-memory-mcp integration (KNOWLEDGE_V3_CODE_GRAPH.md).

Structural knowledge is derived, never stored: every workspace clone gets
its own code-graph index (SQLite cache inside the clone, removed with it)
and agents query it over MCP instead of grepping for structure.

Lives in infrastructure because both the sandbox lifecycle (indexing at
workspace creation) and the executor MCP bridge (server config per run)
need it — always optional: a missing binary just means runs proceed
without code-graph tools.
"""

from __future__ import annotations

from pathlib import Path

from logging_config import logger


def code_graph_binary() -> str:
    """Absolute path of the codebase-memory-mcp binary, "" when absent.

    The binary is bundled into the backend/worker image (Dockerfile); local
    dev without it just runs without the code graph.
    """
    import shutil

    try:
        from config import settings

        configured = (getattr(settings, "OPENSWEEP_CODE_GRAPH_BIN", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured if Path(configured).exists() else ""
    return shutil.which("codebase-memory-mcp") or ""


def _env_for(workspace_path: str) -> dict[str, str]:
    return {
        "CBM_CACHE_DIR": str(Path(workspace_path) / ".opensweep-code-graph"),
        "CBM_ALLOWED_ROOT": workspace_path,
    }


def code_graph_available(workspace_path: str) -> bool:
    """True when a run over this workspace can be given code-graph tools —
    the gate every prompt-briefing and server-config site must share, so an
    agent is never TOLD about tools it doesn't have (or vice versa)."""
    return bool(workspace_path) and bool(code_graph_binary())


def code_graph_server_config(workspace_path: str) -> dict | None:
    """stdio MCP server entry for the code graph over one workspace.

    The SQLite cache (CBM_CACHE_DIR) lives inside the workspace so teardown
    removes it; CBM_ALLOWED_ROOT confines indexing to the clone.
    """
    if not code_graph_available(workspace_path):
        return None
    return {
        "command": code_graph_binary(),
        "args": [],
        "env": _env_for(workspace_path),
    }


def code_graph_opencode_server(workspace_path: str) -> dict | None:
    """opencode.json `mcp` entry for the code graph (local/stdio transport).

    Same server as `code_graph_server_config`, in opencode's config shape:
    command is a list and env is spelled `environment`.
    """
    if not code_graph_available(workspace_path):
        return None
    return {
        "type": "local",
        "command": [code_graph_binary()],
        "enabled": True,
        "environment": _env_for(workspace_path),
    }


async def index_code_graph(workspace_path: str, *, timeout_seconds: int = 120) -> bool:
    """Index a workspace clone so the agent's first structural query is
    instant. Called from sandbox creation (covers recreation too) —
    non-fatal: a missing binary or a failed index just means the run
    proceeds without the code graph."""
    import asyncio

    binary = code_graph_binary()
    if not binary or not workspace_path:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "cli",
            "index_repository",
            "--repo-path",
            workspace_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**_env_for(workspace_path), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        ok = proc.returncode == 0
        if not ok:
            logger.warning(
                f"code graph index exited {proc.returncode} for {workspace_path}",
                extra={"tag": "code-graph"},
            )
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"code graph index failed for {workspace_path}: {type(exc).__name__}: {exc}",
            extra={"tag": "code-graph"},
        )
        return False


# One-paragraph tool briefing for executor system prompts. Kept here so the
# prompt text and the server name ("code-graph") stay in one place.
CODE_GRAPH_PROMPT = """This workspace has a pre-indexed code graph exposed as the `code-graph` MCP
server (tools: search_graph, trace_path, query_graph, get_architecture,
get_code_snippet, search_code, detect_changes, index_repository). For
STRUCTURAL questions —
who calls X, call chains, where a symbol is defined, module/route
inventories, architecture overviews — query the graph FIRST; it is faster
and more complete than grepping files. Use your native Read/Grep for actual
file contents and non-code text. If graph queries return empty results or an
index error, the pre-index may have failed — call index_repository on the
workspace root once, then retry. If the code-graph tools are missing from
your tool list, fall back to Read/Grep."""
