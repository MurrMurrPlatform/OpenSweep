"""Allowlist env builder for agent CLI subprocesses (§6, §13).

Agent CLIs run with `--permission-mode bypassPermissions` inside sandbox
clones — anything in their environment is readable by the code they execute.
The platform process, however, holds real secrets (NEO4J_PASSWORD,
GITHUB_WEBHOOK_SECRET, OPENSWEEP_AUTH_TOKEN, GITHUB_TOKEN, …), so copying
os.environ and stripping known-bad names is the wrong default: every new
platform secret would leak until someone remembers to add it to the strip
list. Instead the agent env is built from an explicit ALLOWLIST plus the
credentials each executor deliberately passes.
"""

from __future__ import annotations

import os

# Vars an agent CLI legitimately needs: process basics + locale + the bits
# node/npx resolve at runtime (opencode still spawns `npx mcp-remote`; claude
# connects to the platform mount over SSE directly).
# nvm-managed node works through PATH alone; NVM_* only refine resolution.
AGENT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    # node / npx (MCP bridge transport)
    "NPM_CONFIG_CACHE",
    "NODE_PATH",
    "NVM_DIR",
    "NVM_BIN",
)


def build_agent_env(*, run_uid: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
    """Compose a minimal agent subprocess env.

    - allowlisted vars copied from os.environ (only when set),
    - IS_SANDBOX=1 (claude refuses bypassPermissions as root without it;
      every agent invocation runs inside a disposable sandbox clone),
    - OPENSWEEP_RUN_UID when a run uid is given,
    - `extra`: credentials the caller explicitly passes through
      (CLAUDE_CODE_OAUTH_TOKEN, provider api keys, …).
    """
    env = {name: os.environ[name] for name in AGENT_ENV_ALLOWLIST if os.environ.get(name)}
    env["IS_SANDBOX"] = "1"
    if run_uid:
        env["OPENSWEEP_RUN_UID"] = run_uid
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env
