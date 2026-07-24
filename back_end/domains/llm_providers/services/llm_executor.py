"""Run an LLMProvider against a (system_prompt, instruction) pair.

The executor is intentionally small: it renders the provider-specific transport
(CLI args for subscription/CLI kinds, JSON body for OpenAI-compatible HTTP kinds),
runs it, and returns the raw transcript. Parsing model output into Candidates,
triage decisions, etc. is the caller's job.

Supported kinds:
    claude_subscription / codex_subscription
        → subprocess, using `cli_command_template`. Placeholders:
            {{system_prompt}}, {{instruction}}, {{model}}      (raw)
            {{system_prompt_q}}, {{instruction_q}}, {{model_q}} (shlex-quoted)
    claude_api / openai_api / mlx / lmstudio / ollama
        → POST {base_url}/chat/completions with the OpenAI Chat shape.
    custom
        → if cli_command_template is set, treat as CLI; otherwise as HTTP.

The result always carries enough context for the UI to show what happened
(rendered prompt, command/url, raw stdout/stderr, exit code, duration_ms).
"""

import asyncio
import json
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from domains.llm_providers.models import LLMProvider
from domains.llm_providers.schemas import default_cli_template
from domains.llm_providers.services.credentials import provider_secret
from infrastructure.process_tree import kill_tree, process_group_kwargs
from logging_config import logger

_HTTP_KINDS = {"claude_api", "openai_api", "mlx", "lmstudio", "ollama"}
_CLI_KINDS = {"claude_subscription", "codex_subscription", "opencode", "aider"}
# Tool-shaped agents read files themselves; the caller should pass a working
# directory and not bother inlining file samples in the prompt.
# `claude_subscription` belongs here because Claude Code's headless mode
# defaults to its full tool suite (Read/Glob/Grep/Bash/Edit + any --mcp-config
# servers). codex_subscription gets MCP servers too (opensweep + code-graph, via
# `-c` overrides in _with_codex_mcp_overrides) but `exec --json` is one-shot.
_TOOL_AGENT_KINDS = {"opencode", "aider", "claude_subscription"}
# Provider kinds that run free, on the user's own machine. Wall-time ceilings
# do not apply to these — a slow local model is fine, the user is paying with
# their own electricity, not metered API tokens. See `is_local_provider_kind`.
_LOCAL_PROVIDER_KINDS = {"mlx", "lmstudio", "ollama", "opencode", "aider"}


def is_local_provider_kind(kind: str) -> bool:
    """True for providers that run on the user's machine and cost nothing.

    Local providers bypass the wall-time ceiling: the platform's reason to
    cut a run short is metered cost or shared infra contention, neither of
    which applies on the user's own laptop.
    """
    return (kind or "").strip() in _LOCAL_PROVIDER_KINDS


@dataclass
class LLMInvocation:
    """The artefacts of a single model call — everything the UI needs to render."""
    raw_output: str = ""
    reasoning: str = ""            # chain-of-thought (Qwen3 reasoning_content)
    stderr: str = ""
    exit_code: int | None = None
    transport: str = ""            # "cli" or "http"
    command_excerpt: str = ""      # rendered argv (CLI) or URL+model (HTTP)
    rendered_system_prompt: str = ""
    rendered_instruction: str = ""
    duration_ms: int = 0
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        # `error` is set by both transports on any real failure (non-zero CLI
        # exit, HTTP >= 400, timeout, transport exception). For HTTP, exit_code
        # is the status code (200 is success, not failure), so trust `error`.
        return not self.error


async def invoke(
    provider: LLMProvider,
    *,
    system_prompt: str,
    instruction: str,
    timeout_seconds: int | None = 180,
    working_dir: str | None = None,
    on_chunk: Optional["StreamCallback"] = None,
    run_uid: str = "",
    extra_cli_args: list[str] | None = None,
    api_overrides: dict | None = None,
) -> LLMInvocation:
    """Run the provider once and return the transcript.

    Pass `working_dir` for tool-shaped agents (opencode/aider) so they can read
    files in the repo. CLI-subscription agents (claude/codex) also honour cwd
    when set, which lets them resolve relative file references.

    `timeout_seconds=None` disables the wall-time guard entirely. Callers use
    this for local providers (MLX/LMStudio/Ollama/opencode/aider) where the
    only cost is the user's own CPU.

    `on_chunk` is an optional async callback invoked as output arrives so
    callers can stream partial results (e.g. update AgentRun.raw_output every
    few hundred ms). Signature: `await on_chunk(stream, partial_text)` where
    `stream` is "stdout" or "stderr" and `partial_text` is the full running
    total for that stream. Throttling is the caller's job — we fire it on every
    chunk that arrives.

    `extra_cli_args` are appended to the rendered argv (CLI transport only —
    e.g. codex reasoning `-c` overrides). `api_overrides` merge into the HTTP
    request overrides (same keys as extra_args JSON; e.g. `reasoning_effort`,
    `suppress_thinking`) and win over the provider's stored extra_args.

    Never raises for transport errors — they're returned as `error` on the
    invocation so callers can persist a failed AgentRun cleanly.
    """
    kind = (provider.kind or "").strip()
    inv = LLMInvocation(
        rendered_system_prompt=system_prompt,
        rendered_instruction=instruction,
    )
    started = time.monotonic()
    try:
        if kind in _CLI_KINDS or (kind == "custom" and provider.cli_command_template):
            await _run_cli(provider, system_prompt, instruction, inv, timeout_seconds, working_dir, on_chunk, run_uid, extra_cli_args)
        elif kind in _HTTP_KINDS or kind == "custom":
            await _run_http(provider, system_prompt, instruction, inv, timeout_seconds, on_chunk, api_overrides)
        else:
            inv.error = f"unsupported provider kind: {kind!r}"
    except TimeoutError:
        inv.error = f"timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        inv.error = f"{type(exc).__name__}: {exc}"[:500]
    inv.duration_ms = int((time.monotonic() - started) * 1000)
    return inv


# Callback signature: `await on_chunk(stream_name, running_total_text)`.
# Awaitable; should be cheap and non-throwing — exceptions are swallowed.
StreamCallback = "Callable[[str, str], Awaitable[None]]"


def is_tool_agent(kind: str) -> bool:
    """True if the provider is a coding-agent CLI that reads files itself
    (opencode, aider). Callers should hand it cwd and skip file sampling."""
    return (kind or "").strip() in _TOOL_AGENT_KINDS


# ── CLI transport ─────────────────────────────────────────────────────────


async def _run_cli(
    provider: LLMProvider,
    system_prompt: str,
    instruction: str,
    inv: LLMInvocation,
    timeout_seconds: int | None,
    working_dir: str | None = None,
    on_chunk=None,
    run_uid: str = "",
    extra_cli_args: list[str] | None = None,
) -> None:
    # Platform-owned fallback: rows saved without a template (pre-defaulting
    # UI, or cleared by hand) still run with the catalog default for the kind.
    template = (provider.cli_command_template or "").strip() or default_cli_template(
        provider.kind or ""
    )
    if not template:
        inv.error = "cli_command_template is empty"
        return

    # Tool agents with their own MCP support get a per-run config file. The
    # CLI template references it via {{mcp_config_path}} (or the shell-quoted
    # variant). Empty string is fine for kinds that don't use it.
    mcp_config_path = ""
    if (provider.kind or "").strip() == "claude_subscription":
        mcp_config_path = _prepare_claude_mcp_config(provider, run_uid=run_uid)

    rendered = _render_template(
        template,
        system_prompt=system_prompt,
        instruction=instruction,
        model=provider.model or "",
        working_dir=working_dir or "",
        mcp_config_path=mcp_config_path,
    )
    extra = (provider.extra_args or "").strip()
    if extra and not _looks_like_json(extra):
        # extra_args is sometimes JSON for HTTP providers; only append for CLI
        # if it's clearly a CLI snippet.
        rendered = f"{rendered} {extra}"

    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        inv.error = f"failed to parse rendered CLI command: {exc}"
        inv.command_excerpt = rendered[:1000]
        return

    if (provider.kind or "").strip() == "codex_subscription":
        # codex has no config-file flag, so the per-run MCP servers (opensweep
        # platform tools + code graph) are injected as `-c` overrides into
        # the operator's template-rendered argv.
        argv = _with_codex_mcp_overrides(argv, run_uid=run_uid, working_dir=working_dir or "")
        # Bypass codex's own OS sandbox + approval prompts — OpenSweep already
        # isolates the run (see with_codex_sandbox_bypass). Injected at runtime so
        # providers seeded before this flag existed are fixed without a re-seed.
        argv = with_codex_sandbox_bypass(argv)

    argv = with_model_flag(
        argv, kind=(provider.kind or "").strip(), model=provider.model or "", template=template
    )
    if extra_cli_args:
        # Caller-supplied per-run flags (e.g. codex `-c model_reasoning_effort=…`).
        argv = [*argv, *extra_cli_args]

    inv.transport = "cli"
    cwd_label = f" (cwd: {working_dir})" if working_dir else ""
    inv.command_excerpt = (" ".join(shlex.quote(a) for a in argv) + cwd_label)[:2000]

    env = _build_cli_env(provider, run_uid=run_uid, working_dir=working_dir or "")

    # `limit` raises the StreamReader buffer cap from the asyncio default
    # (64KB) to 16MB. Claude `--output-format stream-json` events can be huge
    # — a single `tool_result` from a Bash `find` over a large repo regularly
    # exceeds 64KB and triggers `ValueError: Separator is found, but chunk is
    # longer than limit` from readline().
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=working_dir or None,
        limit=16 * 1024 * 1024,
        # Group leader, so the timeout kill reaches the CLI's MCP bridge
        # (npx/mcp-remote) and Bash-tool children too (see process_tree).
        **process_group_kwargs(),
    )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    async def _pump(stream, parts, name):
        """Read line-by-line so coding-agent CLIs (which narrate as they work)
        surface progress to the UI mid-run instead of all at once on exit."""
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            parts.append(text)
            if on_chunk is not None:
                try:
                    await on_chunk(name, "".join(parts))
                except Exception:
                    # Streaming callbacks must never break the run — swallow
                    # and continue.
                    pass

    pumps = asyncio.gather(
        _pump(proc.stdout, stdout_parts, "stdout"),
        _pump(proc.stderr, stderr_parts, "stderr"),
        proc.wait(),
    )
    try:
        if timeout_seconds is None:
            await pumps
        else:
            await asyncio.wait_for(pumps, timeout=timeout_seconds)
    except TimeoutError:
        kill_tree(proc)
        try:
            await proc.wait()
        except Exception:
            pass
        # Preserve whatever we collected before the kill so the user sees
        # partial output instead of a blank panel.
        inv.raw_output = "".join(stdout_parts)
        inv.stderr = "".join(stderr_parts)
        raise

    inv.raw_output = "".join(stdout_parts)
    inv.stderr = "".join(stderr_parts)
    inv.exit_code = proc.returncode
    if proc.returncode != 0 and not inv.error:
        inv.error = f"CLI exited {proc.returncode}"


def _looks_like_json(s: str) -> bool:
    s = s.strip()
    return s.startswith("{") or s.startswith("[")


# Hardcoded opencode provider name used in the auto-generated config. Keep in
# sync with the seed/UI default model: `opensweep/<model-id>`. We pin it (rather
# than deriving from LLMProvider kind) so the model string in the UI is
# predictable across MLX / LMStudio / Ollama / anything OpenAI-compatible.
_OPENCODE_GENERATED_PROVIDER_NAME = "opensweep"


def _mcp_remote_args(*, run_uid: str) -> list[str]:
    """Shared stdio bridge argv — lives in mcp_bridge; lazy import because
    the executors domain imports back into llm_providers at call time."""
    from domains.executors.mcp_bridge import mcp_remote_args

    return mcp_remote_args(run_uid=run_uid)


def with_model_flag(argv: list[str], *, kind: str, model: str, template: str) -> list[str]:
    """Inject the effective model into a claude/codex CLI argv.

    The seeded claude/codex templates have no {{model}} placeholder — without
    this the CLI silently runs its own default model, ignoring the provider's
    model and any per-stage workflow override. Templates that reference
    {{model}} (or already pass a model flag) are left alone.
    """
    model = (model or "").strip()
    if not model or "{{model" in template:
        return argv
    if kind == "claude_subscription" and "--model" not in argv:
        return [*argv, "--model", model]
    if kind == "codex_subscription" and "--model" not in argv and "-m" not in argv:
        try:
            at = argv.index("exec") + 1
        except ValueError:
            at = len(argv)
        return argv[:at] + ["--model", model] + argv[at:]
    return argv


_CODEX_SANDBOX_BYPASS = "--dangerously-bypass-approvals-and-sandbox"

# Any of these in the argv means the operator already chose a sandbox/approval
# policy — don't override it.
_CODEX_SANDBOX_FLAGS = (_CODEX_SANDBOX_BYPASS, "--sandbox", "-s", "--ask-for-approval", "-a", "--full-auto")


def with_codex_sandbox_bypass(argv: list[str]) -> list[str]:
    """Insert codex's external-sandbox bypass right after `exec`.

    OpenSweep runs every agent inside a disposable workspace clone in a
    locked-down worker container — the same rationale as claude's
    `--permission-mode bypassPermissions`. Codex otherwise spins up its OWN OS
    sandbox (landlock/seccomp/bwrap), which cannot create a user namespace in the
    worker ("No permissions to create a new namespace"), so every shell command
    dies; and its non-interactive approval policy has no one to approve, so MCP
    tool calls come back as "user cancelled MCP tool call". This one flag fixes
    both.

    Idempotent, and inert if the argv isn't `codex … exec …` or already carries a
    sandbox/approval flag (an operator template wins).
    """
    if any(flag in argv for flag in _CODEX_SANDBOX_FLAGS):
        return argv
    try:
        at = argv.index("exec") + 1
    except ValueError:
        return argv
    return argv[:at] + [_CODEX_SANDBOX_BYPASS] + argv[at:]


def _with_codex_mcp_overrides(argv: list[str], *, run_uid: str, working_dir: str) -> list[str]:
    """Insert `-c key=value` MCP overrides after codex's `exec` subcommand.

    The template is operator-authored free text, so this only fires when the
    argv is recognizably `codex … exec …`; anything else passes through
    untouched (a run without MCP beats a run that can't start).
    """
    from domains.executors.mcp_bridge import codex_mcp_overrides

    overrides = codex_mcp_overrides(run_uid=run_uid, workspace_path=working_dir)
    if not overrides:
        return argv
    try:
        at = argv.index("exec") + 1
    except ValueError:
        logger.warning("codex template has no `exec` subcommand — skipping MCP overrides")
        return argv
    flags: list[str] = []
    for override in overrides:
        flags += ["-c", override]
    return argv[:at] + flags + argv[at:]


def _prepare_claude_mcp_config(provider: "LLMProvider", *, run_uid: str) -> str:
    """Write a per-run claude mcp-config JSON file and return its absolute path.

    Claude Code's headless mode accepts `--mcp-config <path>` where path is a
    JSON file describing one or more MCP servers. We use the same `mcp-remote`
    stdio bridge as opencode so the in-container `claude` CLI can talk to
    fastapi-mcp's SSE endpoint at /mcp/platform, and we forward
    `X-OpenSweep-Run-Uid` via mcp-remote's `--header` flag so platform tool calls
    land with the right provenance.

    `run_uid` is the Run uid.
    """
    if not run_uid:
        return ""
    config_dir = f"/tmp/opensweep-claude-{run_uid}"
    try:
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "mcp.json")
        payload = {
            "mcpServers": {
                "opensweep": {
                    "command": "npx",
                    "args": _mcp_remote_args(run_uid=run_uid),
                },
            },
        }
        with open(config_path, "w") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        return ""
    return config_path


def _prepare_opencode_config(
    provider: "LLMProvider", *, run_uid: str = "", working_dir: str = ""
) -> str:
    """Write a fresh opencode.json from the LLMProvider row and return XDG_CONFIG_HOME.

    Schema reference: https://opencode.ai/config.json. We register:
      - a single openai-compatible provider named `opensweep` whose `baseURL`
        points at the upstream LLM provider configured in OpenSweep.
      - an `mcp` server entry pointing at /mcp/platform with an
        `X-OpenSweep-Run-Uid` header.

    The model id is the part of `provider.model` after the slash (so
    `opensweep/Qwen3.6-35B-A3B-4bit` → model id `Qwen3.6-35B-A3B-4bit`).

    `run_uid` is the Run uid.
    Falls back to per-provider keying if no check uid is supplied.
    """
    base_url = (provider.base_url or "").strip()
    raw_model = (provider.model or "").strip()
    if not base_url or not raw_model:
        return ""

    # Strip the `<provider-prefix>/` so we have just the model id for the
    # generated config. opencode resolves `opensweep/<model>` against this id.
    model_id = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model

    provider_uid = getattr(provider, "uid", "") or "default"
    key = run_uid or f"provider-{provider_uid}"
    base_dir = f"/tmp/opensweep-opencode-{key}"
    config_dir = os.path.join(base_dir, "opencode")
    try:
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "opencode.json")
        proxied_base_url = base_url
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                _OPENCODE_GENERATED_PROVIDER_NAME: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": f"OpenSweep-managed ({provider.label})",
                    "options": {"baseURL": proxied_base_url},
                    "models": {model_id: {}},
                },
            },
        }
        mcp: dict = {}
        if run_uid:
            # Wire the OpenSweep MCP server through `mcp-remote` so opencode sees
            # the opensweep_* tools. We bridge via stdio because fastapi-mcp 0.4
            # only ships an SSE transport and opencode's "remote" mode has
            # compatibility issues with SSE. `mcp-remote` is an npm package
            # (installed in Dockerfile.dev) that speaks SSE upstream and stdio
            # to its parent — opencode launches it as a subprocess.
            #
            # X-OpenSweep-Run-Uid is forwarded via mcp-remote's --header flag.
            # so every opensweep_file_finding call lands with the right check
            # provenance (resolved server-side; agent can't forge).
            mcp["opensweep"] = {
                "type": "local",
                "command": ["npx", *_mcp_remote_args(run_uid=run_uid)],
                "enabled": True,
            }
        if working_dir:
            # Code-graph MCP over the workspace clone (indexed at sandbox
            # creation) — same server claude_code gets via mcp.json.
            from infrastructure.code_graph import code_graph_opencode_server

            graph = code_graph_opencode_server(working_dir)
            if graph is not None:
                mcp["code-graph"] = graph
        if mcp:
            payload["mcp"] = mcp
        with open(config_path, "w") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        return ""
    return base_dir


def _render_template(template: str, *, system_prompt: str, instruction: str,
                     model: str, working_dir: str = "",
                     mcp_config_path: str = "") -> str:
    replacements = {
        "{{system_prompt_q}}": shlex.quote(system_prompt),
        "{{instruction_q}}": shlex.quote(instruction),
        "{{model_q}}": shlex.quote(model),
        "{{working_dir_q}}": shlex.quote(working_dir),
        "{{mcp_config_path_q}}": shlex.quote(mcp_config_path),
        "{{system_prompt}}": system_prompt,
        "{{instruction}}": instruction,
        "{{model}}": model,
        "{{working_dir}}": working_dir,
        "{{mcp_config_path}}": mcp_config_path,
    }
    out = template
    for needle, value in replacements.items():
        out = out.replace(needle, value)
    return out


def _build_cli_env(provider: LLMProvider, *, run_uid: str = "", working_dir: str = "") -> dict:
    """Allowlist env for the CLI subprocess (§6/§13).

    Agent CLIs execute repo code with tool access inside the sandbox clone,
    so anything in their environment is readable by that code. The child env
    is therefore built from `agent_env.build_agent_env`'s explicit allowlist
    plus the credentials this provider deliberately passes — never an
    `os.environ` copy, which would hand every platform secret
    (NEO4J_PASSWORD, OPENSWEEP_AUTH_TOKEN, GITHUB_TOKEN, …) to the agent.
    Same rule as `mcp_bridge.claude_env` / `turn_cli.codex_turn_env`.
    """
    # Late import — the executors domain imports back into llm_providers.
    from domains.executors.agent_env import build_agent_env

    extra: dict[str, str] = {}
    secret = provider_secret(provider)
    kind = (provider.kind or "").strip()
    base_url = (provider.base_url or "").strip()

    if kind == "claude_subscription":
        if secret:
            extra["CLAUDE_CODE_OAUTH_TOKEN"] = secret
        # IS_SANDBOX=1 (Claude's bypassPermissions-as-root escape hatch) is
        # set by build_agent_env for every agent invocation.

    if kind == "aider":
        # aider's OpenAI-compat path: OPENAI_API_BASE + OPENAI_API_KEY. For local
        # servers, the key can be any non-empty string.
        if base_url:
            extra["OPENAI_API_BASE"] = base_url
            extra["OPENAI_BASE_URL"] = base_url   # newer openai-python also reads this
        extra["OPENAI_API_KEY"] = secret or os.environ.get("OPENAI_API_KEY") or "local-dev"

    if kind == "opencode":
        # Generate opencode.json from this LLMProvider row + the current run_uid
        # and point opencode at it via XDG_CONFIG_HOME. The generated config
        # wires opencode → OpenSweep's LLM proxy (per-run URL for trace capture)
        # and registers OpenSweep's MCP server (per-run header for candidate
        # provenance). No host bind-mount, no user setup.
        xdg = _prepare_opencode_config(provider, run_uid=run_uid, working_dir=working_dir)
        if xdg:
            extra["XDG_CONFIG_HOME"] = xdg
        # The underlying openai SDK boots even when we're not calling OpenAI;
        # set a placeholder so it doesn't complain about a missing key.
        extra["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY") or secret or "local-dev"

    if provider.api_key_env:
        # Operator-declared credential env var set on the worker container —
        # a named, deliberate pass-through (the allowlist model), not an
        # inherited-wholesale environment.
        name = (provider.api_key_env or "").strip()
        value = os.environ.get(name, "") if name else ""
        if value:
            extra[name] = value

    if kind == "codex_subscription":
        # Seed the UI-stored auth.json into a worker-private CODEX_HOME and point
        # codex at it (HOME/CODEX_HOME), exactly as the interactive turn path does
        # via turn_cli.codex_turn_env. Without this, a run launched from Ask / Area
        # Map / actions gets no seeded credential and codex falls back to the
        # worker's stale ~/.codex, failing with "access token could not be
        # refreshed". The per-credential lease + rotation write-back is applied by
        # the executor around the whole run (codex_credential.codex_credential_txn).
        from domains.llm_providers.services.runtime_env import apply_runtime_to_env, build_runtime

        runtime = build_runtime(provider)
        extra.update(runtime.env_vars)
        env = build_agent_env(run_uid=run_uid, extra=extra)
        return apply_runtime_to_env(runtime, env)

    return build_agent_env(run_uid=run_uid, extra=extra)


# ── HTTP transport (OpenAI-compatible /chat/completions) ──────────────────


async def _run_http(
    provider: LLMProvider,
    system_prompt: str,
    instruction: str,
    inv: LLMInvocation,
    timeout_seconds: int | None,
    on_chunk=None,
    api_overrides: dict | None = None,
) -> None:
    base = (provider.base_url or "").rstrip("/")
    if not base:
        inv.error = "base_url is empty"
        return

    overrides = _parse_extra_args(provider.extra_args or "")
    # Caller-supplied per-run overrides (e.g. reasoning knobs) win over the
    # provider's stored extra_args; merged before the pops below so keys like
    # suppress_thinking take effect rather than passing through verbatim.
    overrides.update(api_overrides or {})
    # Reasoning models burn tokens on chain-of-thought before they emit `content`.
    # 8192 gives them ~2-3 KB of thinking *plus* a JSON answer. Override per
    # provider via extra_args={"max_tokens": …}.
    max_tokens = int(overrides.pop("max_tokens", 8192))
    temperature = float(overrides.pop("temperature", 0.2))
    stream = bool(overrides.pop("stream", True))
    suppress_thinking = bool(overrides.pop("suppress_thinking", True))

    # Reasoning models (Qwen3/Qwen3.x "thinking" variants, DeepSeek-R1, etc.) blow
    # past any sane token budget if the chain-of-thought isn't suppressed. The
    # `/no_think` token is the documented kill-switch for Qwen3 — appending it to
    # the system + user messages is harmless for non-thinking models.
    if suppress_thinking and _looks_like_thinking_model(provider.model or ""):
        system_prompt = (system_prompt or "") + "\n/no_think"
        instruction = (instruction or "") + "\n/no_think"

    url = f"{base}/chat/completions"
    payload: dict = {
        "model": provider.model or "",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction},
        ],
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Anything else in extra_args is passed through verbatim (eg. {"top_p": 0.9}).
    payload.update(overrides)

    headers = {"Content-Type": "application/json"}
    api_key = _resolve_api_key(provider)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    inv.transport = "http"
    inv.command_excerpt = (
        f"POST {url} model={provider.model or '-'} max_tokens={max_tokens} "
        f"stream={'on' if stream else 'off'}"
    )
    # Mirror rendered prompts back onto the invocation in case the caller wants
    # to compare what was actually sent (post /no_think mutation) vs the original.
    inv.rendered_system_prompt = system_prompt
    inv.rendered_instruction = instruction

    # httpx >= 0.27: split connect from read so a slow server fails on connect
    # quickly but is allowed to keep streaming tokens up to the wall budget.
    # timeout_seconds=None disables the read/write/pool timeouts for local
    # providers; the connect cap still applies so a wrong base_url fails fast.
    http_timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    try:
        if stream:
            await _stream_chat_completion(url, headers, payload, inv, http_timeout, on_chunk)
        else:
            await _blocking_chat_completion(url, headers, payload, inv, http_timeout)
    except httpx.HTTPError as exc:
        inv.error = f"{type(exc).__name__}: {exc}"[:500]


async def _blocking_chat_completion(url, headers, payload, inv, http_timeout):
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
    inv.exit_code = resp.status_code
    inv.raw_output = resp.text
    if resp.status_code >= 400:
        inv.error = f"HTTP {resp.status_code}"
        return
    try:
        data = resp.json()
    except ValueError:
        return
    content = _extract_assistant_content(data)
    reasoning = _extract_assistant_reasoning(data)
    if content is not None:
        inv.extra["full_response"] = data
        inv.raw_output = content
    if reasoning:
        inv.reasoning = reasoning


async def _stream_chat_completion(url, headers, payload, inv, http_timeout, on_chunk=None):
    """Read tokens as they arrive. Read-timeout fires per-chunk, not per-request,
    so a slow model is fine as long as it's *producing* something."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            inv.exit_code = resp.status_code
            if resp.status_code >= 400:
                body = await resp.aread()
                inv.raw_output = body.decode("utf-8", errors="replace")
                inv.error = f"HTTP {resp.status_code}"
                return
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                before_content = len(content_parts)
                _accumulate_delta(delta, content_parts, reasoning_parts)
                if on_chunk is not None and len(content_parts) > before_content:
                    try:
                        await on_chunk("stdout", "".join(content_parts))
                    except Exception:
                        pass
    inv.raw_output = "".join(content_parts)
    inv.reasoning = "".join(reasoning_parts)


def _accumulate_delta(delta: dict, content_parts: list[str], reasoning_parts: list[str]) -> None:
    for key, sink in (("content", content_parts), ("reasoning_content", reasoning_parts)):
        piece = delta.get(key)
        if isinstance(piece, str):
            sink.append(piece)
        elif isinstance(piece, list):
            for part in piece:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    sink.append(part["text"])


def _parse_extra_args(raw: str) -> dict:
    """`extra_args` may be JSON ({"max_tokens": 1024}) or empty. Garbage is ignored."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _looks_like_thinking_model(model: str) -> bool:
    """Heuristic — Qwen3/Qwen3.x families emit chain-of-thought unless told not to.

    Override per-provider by setting extra_args to {"suppress_thinking": false}.
    """
    m = (model or "").lower()
    if "qwen3" in m and "coder" not in m:
        return True
    if "deepseek-r1" in m or "deepseek_r1" in m:
        return True
    if "thinking" in m:
        return True
    return False


def _resolve_api_key(provider: LLMProvider) -> str:
    secret = provider_secret(provider)
    if secret:
        return secret
    env = (provider.api_key_env or "").strip()
    if env:
        return os.environ.get(env, "")
    return ""


def _extract_assistant_content(data: dict) -> str | None:
    """OpenAI chat-completions response → assistant text. Returns None on miss."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        # Some providers return content as a list of parts.
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return None
    except (AttributeError, IndexError, TypeError):
        return None


def _extract_assistant_reasoning(data: dict) -> str | None:
    """Pull `message.reasoning_content` for Qwen3-style thinking models."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in reasoning
            )
        return None
    except (AttributeError, IndexError, TypeError):
        return None


# ── Best-effort JSON extraction ──────────────────────────────────────────


def extract_json_payload(text: str) -> object | None:
    """Pull the first JSON value out of `text`, tolerating fenced blocks and prose.

    Returns None if nothing parseable is found. Used by callers that asked the
    model for "a JSON array of …" and need to be forgiving about wrapping.
    """
    if not text:
        return None
    candidates: list[str] = []
    # 1) ```json … ``` fenced block
    stripped = text.strip()
    if "```" in stripped:
        parts = stripped.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i]
            if block.startswith("json"):
                block = block[4:]
            candidates.append(block.strip())
    candidates.append(stripped)
    for cand in candidates:
        for opener, closer in (("[", "]"), ("{", "}")):
            start = cand.find(opener)
            end = cand.rfind(closer)
            if start != -1 and end != -1 and end > start:
                snippet = cand[start:end + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None
