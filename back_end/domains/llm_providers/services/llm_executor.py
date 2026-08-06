"""Run an LLMProvider against a (system_prompt, instruction) pair.

The executor is intentionally small: it renders the provider's CLI invocation,
runs it, and returns the raw transcript. Parsing model output into Candidates,
triage decisions, etc. is the caller's job.

Supported kinds (all coding-agent harnesses — there is no raw HTTP path):
    claude_subscription / opencode
        → subprocess, using `cli_command_template`. Placeholders:
            {{model}}, {{working_dir}}                          (raw, platform-set)
            {{system_prompt_q}}, {{instruction_q}}, {{model_q}} (shlex-quoted)
        system_prompt/instruction carry untrusted text, so ONLY their
        shlex-quoted _q variants exist — use those.

The result always carries enough context for the UI to show what happened
(rendered prompt, command, raw stdout/stderr, exit code, duration_ms).
"""

import asyncio
import ipaddress
import json
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from domains.llm_providers.models import LLMProvider
from domains.llm_providers.schemas import default_cli_template
from domains.llm_providers.services.credentials import provider_secret
from infrastructure.process_tree import kill_tree, process_group_kwargs

_CLI_KINDS = {"claude_subscription", "opencode"}
# Only opencode can be locality-checked at all — claude_subscription always
# hits Anthropic's hosted API. Locality is decided per-provider (base_url),
# NOT per-kind: an opencode row can point at a genuinely local server OR a
# hosted OpenAI-compatible endpoint (Azure Foundry, OpenRouter, …), and the
# two must not be treated the same for wall-time exemption or `local_only`
# routing. See `is_local_provider`.
_LOCAL_CAPABLE_KINDS = {"opencode"}

# Hostnames considered on-machine (no metered call, no data leaves the box).
# host.docker.internal is Docker's magic name for the host loopback — the
# provider seeds' `default_base_url` uses it, so worker containers reach the
# user's local model server through it.
_LOCAL_HOSTS = {"localhost", "host.docker.internal"}
_LOCAL_SUFFIXES = (".local", ".internal")


def _is_local_hostname(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Loopback (127.0.0.0/8, ::1) + RFC1918 LAN ranges — same box or same LAN
    # is unmetered from a run-policy standpoint.
    return ip.is_loopback or ip.is_private


def is_local_base_url(url: str) -> bool:
    """True when `url` resolves to on-machine / LAN.

    Empty is NOT local: an opencode row without a base_url falls through to
    the container's ambient opencode config (which could point anywhere).
    Defaulting empty to cloud keeps `local_only=True` from silently letting
    an unconfigured row through.
    """
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}", scheme="")
    host = parsed.hostname or ""
    return _is_local_hostname(host)


def is_local_provider(provider) -> bool:
    """True for providers that run on the user's machine and cost nothing.

    Local providers bypass the wall-time ceiling and satisfy `local_only`
    routing: the platform's reasons to cut a run short (metered cost) and
    to refuse cloud dispatch (data leaving the box) both go away on a
    genuinely local endpoint.

    `provider` is duck-typed on `.kind` and `.base_url` — the resolved
    `LLMProvider` row, or a lightweight snapshot with the same attributes.
    """
    if provider is None:
        return False
    kind = (getattr(provider, "kind", "") or "").strip()
    if kind not in _LOCAL_CAPABLE_KINDS:
        return False
    return is_local_base_url(getattr(provider, "base_url", "") or "")


@dataclass
class LLMInvocation:
    """The artefacts of a single model call — everything the UI needs to render."""
    raw_output: str = ""
    reasoning: str = ""
    stderr: str = ""
    exit_code: int | None = None
    transport: str = ""            # "cli"
    command_excerpt: str = ""      # rendered argv
    rendered_system_prompt: str = ""
    rendered_instruction: str = ""
    duration_ms: int = 0
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        # `error` is set on any real failure (non-zero CLI exit, timeout,
        # transport exception).
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
) -> LLMInvocation:
    """Run the provider once and return the transcript.

    Pass `working_dir` so the agent CLI can read files in the repo clone.

    `timeout_seconds=None` disables the wall-time guard entirely. Callers use
    this for local providers (opencode) where the only cost is the user's own
    CPU.

    `on_chunk` is an optional async callback invoked as output arrives so
    callers can stream partial results (e.g. update AgentRun.raw_output every
    few hundred ms). Signature: `await on_chunk(stream, partial_text)` where
    `stream` is "stdout" or "stderr" and `partial_text` is the full running
    total for that stream. Throttling is the caller's job — we fire it on every
    chunk that arrives.

    `extra_cli_args` are appended to the rendered argv.

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
        if kind in _CLI_KINDS:
            await _run_cli(provider, system_prompt, instruction, inv, timeout_seconds, working_dir, on_chunk, run_uid, extra_cli_args)
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
        # extra_args is JSON for opencode config overrides; only append for
        # CLI if it's clearly a CLI snippet.
        rendered = f"{rendered} {extra}"

    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        inv.error = f"failed to parse rendered CLI command: {exc}"
        inv.command_excerpt = rendered[:1000]
        return

    argv = with_model_flag(
        argv, kind=(provider.kind or "").strip(), model=provider.model or "", template=template
    )
    if extra_cli_args:
        # Caller-supplied per-run flags.
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
# predictable across every OpenAI-compatible endpoint.
_OPENCODE_GENERATED_PROVIDER_NAME = "opensweep"


def _mcp_remote_args(*, run_uid: str) -> list[str]:
    """Shared stdio bridge argv — lives in mcp_bridge; lazy import because
    the executors domain imports back into llm_providers at call time."""
    from domains.executors.mcp_bridge import mcp_remote_args

    return mcp_remote_args(run_uid=run_uid)


def with_model_flag(argv: list[str], *, kind: str, model: str, template: str) -> list[str]:
    """Inject the effective model into a claude CLI argv.

    The seeded claude template has no {{model}} placeholder — without this the
    CLI silently runs its own default model, ignoring the provider's model and
    any per-stage workflow override. Templates that reference {{model}} (or
    already pass a model flag) are left alone.
    """
    model = (model or "").strip()
    if not model or "{{model" in template:
        return argv
    if kind == "claude_subscription" and "--model" not in argv:
        return [*argv, "--model", model]
    return argv


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
        # The MCP config's mcp-remote args carry the per-run auth header; keep
        # it off the world-readable /tmp umask. 0600 on the file after write.
        os.chmod(config_dir, 0o700)
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
        os.chmod(config_path, 0o600)  # carries the per-run auth header
    except OSError:
        return ""
    return config_path


def _prepare_opencode_config(
    provider: "LLMProvider", *, run_uid: str = "", working_dir: str = ""
) -> str:
    """Write a fresh opencode.json from the LLMProvider row and return XDG_CONFIG_HOME.

    Schema reference: https://opencode.ai/config.json. We register:
      - a single openai-compatible provider named `opensweep` whose `baseURL`
        points at the upstream LLM endpoint configured in OpenSweep.
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
        # This file holds the provider API key in cleartext; keep it off the
        # default world-readable /tmp umask (0755/0644). 0600 on the file is
        # applied after the write.
        os.chmod(base_dir, 0o700)
        config_path = os.path.join(config_dir, "opencode.json")
        proxied_base_url = base_url
        options: dict = {"baseURL": proxied_base_url}
        # The AI SDK provider packages read the key from `options.apiKey` ONLY —
        # they ignore OPENAI_API_KEY in the environment, so a hosted endpoint
        # answers 401 unless the credential is written into the config. (Local
        # servers accept anything, which is why this went unnoticed.)
        api_key = _resolve_api_key(provider)
        if api_key:
            options["apiKey"] = api_key

        overrides = _parse_extra_args(getattr(provider, "extra_args", "") or "")
        # `@ai-sdk/openai-compatible` always emits `max_tokens`, which reasoning
        # models reject outright (HTTP 400 `unsupported_parameter`). The first-
        # party `@ai-sdk/openai` package knows to send `max_completion_tokens`
        # instead. opencode builds the request, so this is the only lever we
        # have over it. Override with extra_args={"opencode_npm": "..."} — the
        # Azure Foundry endpoint preset sets exactly that.
        reasoning = bool(overrides.get("reasoning", _is_reasoning_model(model_id)))
        npm = str(
            overrides.get("opencode_npm")
            or ("@ai-sdk/openai" if reasoning else "@ai-sdk/openai-compatible")
        )
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                _OPENCODE_GENERATED_PROVIDER_NAME: {
                    "npm": npm,
                    "name": f"OpenSweep-managed ({provider.label})",
                    "options": options,
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
        os.chmod(config_path, 0o600)  # holds the provider API key
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
        # No raw {{system_prompt}} / {{instruction}} variants: those carry
        # LLM- and repo-derived text into the argv that is shlex.split, so an
        # unquoted form is argv injection. Only the shlex-quoted _q variants
        # exist for them; a template using the raw form leaves the literal
        # placeholder (an obvious failure) instead of injecting.
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
    Same rule as `mcp_bridge.claude_env`.
    """
    # Late import — the executors domain imports back into llm_providers.
    from domains.executors.agent_env import build_agent_env

    extra: dict[str, str] = {}
    secret = provider_secret(provider)
    kind = (provider.kind or "").strip()

    if kind == "claude_subscription":
        if secret:
            extra["CLAUDE_CODE_OAUTH_TOKEN"] = secret
        # IS_SANDBOX=1 (Claude's bypassPermissions-as-root escape hatch) is
        # set by build_agent_env for every agent invocation.

    if kind == "opencode":
        # Generate opencode.json from this LLMProvider row + the current run_uid
        # and point opencode at it via XDG_CONFIG_HOME. The generated config
        # wires opencode → the configured endpoint and registers OpenSweep's
        # MCP server (per-run header for candidate provenance). No host
        # bind-mount, no user setup.
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

    return build_agent_env(run_uid=run_uid, extra=extra)


def _parse_extra_args(raw: str) -> dict:
    """`extra_args` may be JSON ({"opencode_npm": …}) or empty. Garbage is ignored."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_reasoning_model(model: str) -> bool:
    """Heuristic — OpenAI reasoning families constrain the request shape:
    they take `max_completion_tokens` (never `max_tokens`), a hard HTTP 400 on
    Azure, not a silent ignore. Drives the @ai-sdk package choice in
    `_prepare_opencode_config`.

    Matched loosely because the value may be an Azure *deployment* name rather
    than a model id (`gpt-5-mini-gambit`). Override per-provider with
    extra_args={"reasoning": true|false}.
    """
    m = (model or "").lower().removeprefix("openai/")
    return any(m == p or m.startswith(p + "-") for p in _REASONING_PREFIXES)


def _resolve_api_key(provider: LLMProvider) -> str:
    secret = provider_secret(provider)
    if secret:
        return secret
    env = (getattr(provider, "api_key_env", "") or "").strip()
    if env:
        return os.environ.get(env, "")
    return ""
