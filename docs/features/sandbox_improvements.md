# Sandbox improvements — options landscape

Status: options survey (2026-07-19). Track 3 (terminal takeover, options
3A + 3B) is implemented; the other tracks are candidates for follow-up specs.

## Context: what exists today

- **Sandboxes are plain git clones on the host filesystem**, not containers
  (`back_end/domains/execution/services/sandbox_service.py`). They live at
  `~/.opensweep/sandboxes/<uid>`, are shallow-cloned per run, and are cleaned
  up on a sliding retention window. The agent CLI runs as a subprocess in the
  worker container with a strict environment allowlist
  (`back_end/domains/executors/agent_env.py`) — no platform secrets reach it.
- **Two MCP servers** are wired into every run via a generated `mcp.json`
  (`back_end/domains/executors/mcp_bridge.py`): `opensweep-platform`
  (per-run-token SSE bridge to platform tools) and `code-graph`
  (codebase-memory-mcp over the indexed workspace).
- **Test Locally** (`front_end/src/components/delivery/TestLocallyButton.vue`)
  is a clipboard helper: it copies a `gh pr checkout` / `git worktree add`
  command for the developer to run manually. No backend involvement.
- **Runs persist session state**: `cli_session_id` (Claude Code session),
  `workspace_spec` (enough to recreate a destroyed sandbox), and a durable
  `events.jsonl` transcript (`back_end/domains/investigations/models.py`,
  `run_events.py`).
- There is **no Docker inside sandboxes**, no devcontainer support, no
  per-repo test-runner integration, and no warm pooling/caching beyond
  shallow clones.

## Track 1 — Richer agent capabilities (MCPs, skills, subagents)

Because sandboxes are real git clones, **repo-committed `.claude/` content
(CLAUDE.md, skills, agents) already works for free** — Claude Code picks it up
from the working directory. What is missing is platform-curated capability
injection.

### 1A. Platform capability bundle (recommended, cheap)

Ship a curated `CLAUDE_CONFIG_DIR`-style bundle per run: platform-managed
skills (run the test suite, write a migration, playbook-specific helpers),
subagent definitions, and settings. Generalizes the existing per-run
`/tmp/opensweep-claude-{run_uid}/mcp.json` into a full config bundle,
versioned in this repo so capabilities improve for all users on upgrade.

### 1B. Per-repo MCP registry in Admin UI

Let users register extra MCP servers (Sentry, Linear, context7, read-only
Postgres, …) per repo/workspace with scoped credentials, merged into the
generated `mcp.json`. Requires a secrets story consistent with the current
env-allowlist discipline: credentials reach only that run's process.

### 1C. Discovery-authored capabilities

Sweep agents already build docs and memories — extend them to *author*
repo-specific skills ("how to run this project's tests", "seed data lives
here") that later Delivery runs consume. This closes a loop no competitor
closes and compounds over time.

## Track 2 — Running the stack, tests, and Playwright in sandboxes

A cost ladder; do not jump to full-stack-per-sandbox.

### 2A. Repo capability manifest + native test execution (recommended first)

A per-repo manifest — discovered by a sweep, user-confirmed — declaring
`install`, `lint`, `typecheck`, `unit_test` commands that run directly in the
sandbox directory. No Docker needed; covers static analysis and most test
suites. Cache dependency dirs (`node_modules`, venvs) per repo@lockfile-hash
on the host. This alone captures most of the value.

### 2B. Docker socket + per-repo shared warm stack

Mount the host Docker socket into the worker; bring up **one** long-lived
compose stack *per repo* (namespaced project, dynamic ports, health-checked,
migrations kept current by a maintenance task). Isolation moves to the data
layer: per-run Postgres template-database clones (`CREATE DATABASE …
TEMPLATE golden` is sub-second) or per-run schema prefixes. Pays the
image-build/migrate/seed cost once per repo instead of per sandbox.
Playwright falls out of this: once a stack URL exists, runs get a Playwright
MCP (or the bundled headless browser) pointed at the warm stack.

### 2C. Devcontainer adoption

Honor `.devcontainer/devcontainer.json` when present; build and cache the
image per repo@config-hash. Standards-based but heavier, and only helps repos
that ship one.

### 2D. Full ephemeral stack per sandbox

Genuinely expensive; only worth it for destructive integration tests. An
opt-in "heavy run" tier later, possibly offloaded to remote runners
(Fly Machines / Depot-style) for the hosted product.

## Track 3 — Terminal takeover of Thread conversations

Two facts make this tractable: sandboxes live on the user's host filesystem,
and Runs store `cli_session_id`.

### 3A. True session handoff (implemented)

Shipped as `POST /api/v1/runs/{uid}/handoff` plus the "Continue in terminal"
button on Threads: one paste copies the run's Claude session file under the
host cwd's project slug and runs `claude --resume <id>` inside the sandbox.
Session files are shared through the `~/.claude` bind-mounts (worker and
backend).

The shipped command (copy-to-clipboard, next to Test Locally):

```sh
cd "$HOME/.opensweep/sandboxes/<uid>" && \
  dst="$HOME/.claude/projects/$(pwd -P | sed 's/[^a-zA-Z0-9]/-/g')" && \
  mkdir -p "$dst" && \
  cp -n "$HOME/.claude/projects/-host-sandboxes-<uid>/<sid>.jsonl" "$dst/" 2>/dev/null; \
  claude --resume <sid>
```

The session-file copy is needed because `claude --resume` is project-scoped
(session files are keyed by the cwd's path slug, and container/host cwds
differ). This resumes the *actual* session — full context, same working tree.
Takeover is recorded on the run and thread timelines (not enforced as a
lock); handing back = just resume the conversation in the platform — the
next turn reads the same working copy, local commits included.

### 3B. Transcript-seeded fresh session (implemented, fallback)

Shipped alongside 3A: the handoff endpoint always writes an
`OPENSWEEP_HANDOFF.md` brief (task, branch contract, transcript tail) into the
sandbox root, excluded via `.git/info/exclude`. Non-claude executors — and any
failed resume — start a fresh local `claude` pointed at that brief.

Loses true session state but works when the session store is not shared
(hosted deployments, remote workers, non-Claude executors).

### 3C. Terminal as a Thread client (later)

An `opensweep thread` TUI that streams/sends messages through the backend
while the agent still runs in the platform. Remote control rather than
takeover; a different feature, not the quick win.

## Recommended sequencing

1. **1A + 2A** — config bundle + capability manifest: low-risk, no Docker.
2. **3A (+3B fallback)** — small, high-delight given the pieces already exist.
3. **2B** — warm shared stacks: the big infrastructure investment.
4. **1B / 1C / 2C / 2D / 3C** — follow-ups as demand emerges.
