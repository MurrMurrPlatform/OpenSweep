"""Compose the env / CWD a worker needs to invoke an LLMProvider's CLI.

Reads a stored `credential_secret` off the LLMProvider node and produces:
- `env_vars`: dict to merge into subprocess env
- `extra_files`: list of (path, content, mode) tuples to write before invocation
  (eg. for Codex which needs auth.json on disk).

Falls back to environment variables (`CLAUDE_CODE_OAUTH_TOKEN`, `<api_key_env>`)
when no UI-stored secret is present, so .env-based setups keep working.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field

from domains.llm_providers.models import LLMProvider
from domains.llm_providers.schemas import LLMProviderKind
from domains.llm_providers.services.credentials import provider_secret

_CODEX_HOME_PREFIX = "opensweep-codex-"


@dataclass
class ProviderRuntime:
    env_vars: dict[str, str] = field(default_factory=dict)
    extra_files: list[tuple[str, str, int]] = field(default_factory=list)
    # If set, the worker should run with HOME pointing here (so the CLI looks for
    # its dotfiles under this dir). Used by codex when we write a private auth.json.
    home_override: str | None = None

    def cleanup(self) -> None:
        """Delete the worker-private home dir (it holds `auth.json` — a full
        OAuth blob) once the invocation is done (audit #21).

        Guarded to `opensweep-codex-*` dirs directly under the system temp dir so
        a misconfigured `home_override` can never delete anything else.
        Idempotent and never raises — credential hygiene must not fail a run.
        """
        home = self.home_override
        self.home_override = None
        if not home:
            return
        normalized = os.path.normpath(home)
        if (
            os.path.dirname(normalized) != tempfile.gettempdir()
            or not os.path.basename(normalized).startswith(_CODEX_HOME_PREFIX)
        ):
            return
        shutil.rmtree(normalized, ignore_errors=True)

    def __enter__(self) -> "ProviderRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def _codex_home(provider: LLMProvider) -> str:
    """Deterministic worker-private CODEX_HOME for this provider AT ITS CURRENT
    credential revision. The revision is part of the path so a rotated-revision
    server never overwrites the auth.json a still-running old-revision server is
    using (the app-server registry keeps one server per (uid, revision))."""
    uid = "".join(
        c for c in (getattr(provider, "uid", "") or "") if c.isalnum() or c in "-_"
    ) or "default"
    rev = int(getattr(provider, "credential_revision", 0) or 0)
    return os.path.join(tempfile.gettempdir(), f"{_CODEX_HOME_PREFIX}{uid}-r{rev}")


def apply_runtime_to_env(runtime: ProviderRuntime, env: dict[str, str]) -> dict[str, str]:
    """Write a provider's runtime files to disk and point the CLI at them.

    Shared by the interactive turn path (`turn_cli.codex_turn_env`) and the
    run/workflow path (`llm_executor._build_cli_env`) so codex-subscription
    credentials are seeded identically no matter how the run was launched.

    File writes never raise — a missing auth.json surfaces as codex's own auth
    error, not an opaque crash — and HOME/CODEX_HOME are only set when a
    worker-private home was actually provisioned (a UI-stored auth.json exists;
    bind-mount providers keep the worker's default ~/.codex).
    """
    for path, content, mode in runtime.extra_files:
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, mode)
        except OSError:
            pass
    if runtime.home_override:
        env["HOME"] = runtime.home_override
        env["CODEX_HOME"] = f"{runtime.home_override}/.codex"
    return env


def build_runtime(provider: LLMProvider) -> ProviderRuntime:
    rt = ProviderRuntime()
    secret = provider_secret(provider)
    kind = provider.kind

    if kind == LLMProviderKind.CLAUDE_SUBSCRIPTION.value:
        if secret:
            rt.env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = secret
        elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            rt.env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        return rt

    if kind == LLMProviderKind.CODEX_SUBSCRIPTION.value:
        if secret:
            # Write the auth.json blob to a worker-private CODEX_HOME so we don't
            # touch the host-bind-mounted ~/.codex.
            home = _codex_home(provider)
            os.makedirs(f"{home}/.codex", mode=0o700, exist_ok=True)
            rt.extra_files.append((f"{home}/.codex/auth.json", secret, 0o600))
            rt.home_override = home
        # else: fall through and use whatever's in the bind-mounted /root/.codex
        return rt

    if kind in (LLMProviderKind.CLAUDE_API.value, LLMProviderKind.OPENAI_API.value):
        env_name = provider.api_key_env or (
            "ANTHROPIC_API_KEY" if kind == LLMProviderKind.CLAUDE_API.value else "OPENAI_API_KEY"
        )
        if secret:
            rt.env_vars[env_name] = secret
        elif os.environ.get(env_name):
            rt.env_vars[env_name] = os.environ[env_name]
        return rt

    # MLX / LMStudio / Ollama / custom — no credential plumbing needed.
    return rt
