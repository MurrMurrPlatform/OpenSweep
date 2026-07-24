"""MCP bridge config generators for delegated executors.

PLATFORM.md §Executor return contracts: the *preferred* path is for the
executor to call platform tools natively. For Claude Code we expose the
tracking-safe platform write tools through the fastapi-mcp mount, then write a
per-run `mcp.json` for the `claude` CLI's `--mcp-config` flag.

`X-OpenSweep-Run-Uid` is forwarded on the MCP connection so the platform
side can correlate tool calls back to the source Run.
"""

from __future__ import annotations

import json
from pathlib import Path

from domains.executors.agent_env import build_agent_env
from infrastructure.code_graph import code_graph_codex_overrides, code_graph_server_config
from infrastructure.run_tokens import mint_run_token

# Startup budget (ms) the claude CLI grants each MCP server before marking it
# failed. The HTTP handshake to the platform mount can lag behind the CLI's
# first turn; a generous ceiling keeps a slow connect from stranding the run
# without opensweep-platform tools.
_MCP_STARTUP_TIMEOUT_MS = "60000"


def opensweep_auth_token() -> str:
    """The shared auth token in-cluster callers must present when set.

    Late import — config has its own bootstrap order."""
    try:
        from config import settings

        return getattr(settings, "OPENSWEEP_AUTH_TOKEN", "") or ""
    except Exception:
        return ""


def platform_mcp_url() -> str:
    """Absolute in-cluster URL of the platform-tool MCP mount."""
    backend = "http://opensweep_backend:8000"
    mount_path = "/mcp/platform"
    # Late import to avoid hard dependency at module load — config has its
    # own bootstrap order.
    try:
        from config import settings

        backend = (getattr(settings, "OPENSWEEP_BACKEND_INTERNAL_URL", backend) or backend).rstrip("/")
        mount_path = getattr(settings, "MCP_PLATFORM_TOOL_MOUNT_PATH", mount_path) or mount_path
    except Exception:
        pass
    return f"{backend}{mount_path}"


async def probe_platform_mcp(timeout_seconds: float = 3.0) -> str:
    """Return "" when the platform MCP mount is reachable, else a diagnostic.

    Any HTTP response — including 401/405 — proves reachability (auth is the
    agent's problem, not the probe's); only transport failures count. Callers
    use this to fail a run fast with a clear error instead of letting the
    agent start with the opensweep-platform server stuck on "connecting".
    """
    import httpx

    url = platform_mcp_url()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            await client.head(url)
        return ""
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"


def mcp_remote_args(*, run_uid: str) -> list[str]:
    """`mcp-remote` argv (minus the leading npx) for the /mcp/platform bridge.

    The stdio fallback for CLIs that can't speak fastapi-mcp's Streamable HTTP
    transport natively (opencode, codex) — claude connects direct-HTTP via
    `write_claude_mcp_config` instead. mcp-remote negotiates HTTP first and
    falls back to SSE, so it rides the same URL. mcp-remote rejects non-HTTPS URLs
    unless --allow-http is set or the host is literal "localhost" — the
    backend is reachable on the Docker network (OPENSWEEP_BACKEND_INTERNAL_URL)
    but not over TLS. X-OpenSweep-Run-Uid carries run provenance; X-OpenSweep-Auth
    carries the PER-RUN scoped token when auth is enabled
    (TokenAuthMiddleware gates the MCP mounts too) — never the platform-wide
    OPENSWEEP_AUTH_TOKEN, since this argv lands in a config file the agent can
    read (§13).
    """
    args = [
        "-y", "mcp-remote",
        platform_mcp_url(),
        "--allow-http",
        "--header", f"X-OpenSweep-Run-Uid: {run_uid}",
    ]
    run_token = mint_run_token(run_uid)
    if run_token:
        args += ["--header", f"X-OpenSweep-Auth: {run_token}"]
    return args


def codex_mcp_overrides(*, run_uid: str, workspace_path: str = "") -> list[str]:
    """`codex -c key=value` TOML overrides registering the per-run MCP servers.

    codex has no --mcp-config flag, so both servers ride in as config
    overrides: opensweep platform tools through the mcp-remote stdio bridge, plus
    the code graph when the workspace has one. json.dumps output is valid
    TOML for plain strings and string arrays.
    """
    overrides: list[str] = []
    if run_uid:
        overrides += [
            'mcp_servers.opensweep.command="npx"',
            f"mcp_servers.opensweep.args={json.dumps(mcp_remote_args(run_uid=run_uid))}",
        ]
    overrides += code_graph_codex_overrides(workspace_path)
    return overrides


def write_claude_mcp_config(
    *, run_uid: str, scratch_root: str = "/tmp", workspace_path: str = ""
) -> str:
    """Write per-run claude `--mcp-config` JSON and return its absolute path.

    The platform mount is declared as a direct Streamable HTTP server
    (fastapi-mcp `.mount_http()`): the `claude` CLI connects to it natively,
    with no node/npx bridge process in between — one less startup race before
    the tools register.

    When `workspace_path` is set and the codebase-memory-mcp binary is
    available, a second stdio server is added so the agent can answer
    structural questions from the code graph instead of grepping
    (KNOWLEDGE_V3_CODE_GRAPH.md).
    """
    if not run_uid:
        return ""

    config_dir = Path(scratch_root) / f"opensweep-claude-{run_uid}"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "mcp.json"
    headers = {"X-OpenSweep-Run-Uid": run_uid}
    # When auth is on (§13), in-cluster tool calls must carry a credential
    # too — TokenAuthMiddleware gates the MCP mounts as well. The agent gets
    # a PER-RUN scoped token (recomputable from the run uid), NOT the
    # platform-wide OPENSWEEP_AUTH_TOKEN: this file lives where the agent can
    # read it. Minting works whenever a run-token secret exists
    # (OPENSWEEP_RUN_TOKEN_SECRET or OPENSWEEP_AUTH_TOKEN) — Zitadel-only deployments
    # MUST set OPENSWEEP_RUN_TOKEN_SECRET or every executor callback 401s. When
    # auth is off entirely, mint_run_token returns "" → no header.
    run_token = mint_run_token(run_uid)
    if run_token:
        headers["X-OpenSweep-Auth"] = run_token
    payload = {
        "mcpServers": {
            "opensweep-platform": {
                "type": "http",
                "url": platform_mcp_url(),
                "headers": headers,
            },
        },
    }
    if workspace_path:
        graph = code_graph_server_config(workspace_path)
        if graph is not None:
            payload["mcpServers"]["code-graph"] = graph
    config_path.write_text(json.dumps(payload, indent=2))
    return str(config_path)


def codex_mcp_config_object(*, run_uid: str, workspace_path: str = "") -> dict:
    """The per-run MCP servers as a nested config dict for app-server
    `thread/start.config` — same servers as the exec path's `-c` overrides
    (codex_mcp_overrides), just structured instead of flat."""
    import json as _json
    cfg: dict = {}
    for override in codex_mcp_overrides(run_uid=run_uid, workspace_path=workspace_path):
        key, _, raw = override.partition("=")
        try:
            value = _json.loads(raw)          # json.dumps output is the source; round-trips
        except _json.JSONDecodeError:
            value = raw.strip('"')
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return cfg


def claude_env(*, run_uid: str, oauth_token: str = "") -> dict[str, str]:
    """Env vars for a `claude` subprocess invocation.

    SECURITY (§6/§13): the agent process must never see platform secrets
    (GITHUB_TOKEN, NEO4J_PASSWORD, OPENSWEEP_AUTH_TOKEN, …). Built from the
    explicit allowlist in `agent_env` — never a filtered os.environ copy —
    plus the one credential claude needs (its own OAuth token). Sandbox
    clone/push auth travels in per-invocation git extraHeaders on the
    PLATFORM side only.
    """
    extra: dict[str, str] = {"MCP_TIMEOUT": _MCP_STARTUP_TIMEOUT_MS}
    if oauth_token:
        extra["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return build_agent_env(run_uid=run_uid, extra=extra)
