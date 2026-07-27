"""OpenCode write runs — "bring your own agent" beyond Discovery.

Before this, `_WRITE_CAPABLE_EXECUTORS = {CLAUDE_CODE}` meant implement/fix runs
were possible only on a Claude subscription. OpenCode now takes IMPLEMENT runs
too, but ONLY when the model behind it is the user's own local server: an
opencode row is a CLI wrapper, and `is_local_provider_kind("opencode")` answers
True whatever `base_url` points at, so gating on the raw row kind would hand a
write sandbox to an agent talking to a metered cloud endpoint.

What is pinned here:
  - `resolved_provider_kind` maps an opencode row to the local server behind it
    (lmstudio / mlx / ollama) or to "" for anything off-box;
  - the lifecycle gate: OpenCode+LMStudio and OpenCode+MLX may write, while
    OpenCode+cloud, Codex and internal_llm may not, and Claude Code still may;
  - the executor re-resolution that lets a write playbook's hardcoded
    `Executor.CLAUDE_CODE` land on OpenCode when that is what the org has;
  - the adapter's own second check, which refuses an IMPLEMENT run outright
    rather than silently running the read-only prompt;
  - the write prompt/instruction pair, including that the agent is told to
    COMMIT locally and never push (delivery.write_gate does the pushing).
"""

from types import SimpleNamespace

import pytest

from domains.executors import cli_tracking
from domains.executors.base import DispatchRequest
from domains.executors.prompt_kit import system_prompt
from domains.llm_providers.services.llm_executor import (
    is_local_provider_kind,
    resolved_provider_kind,
)
from domains.runs.schemas import ExecutionMode, Executor, RunStatus
from domains.runs.services.lifecycle import (
    _provider_supports_write,
    write_executor_for_provider,
)

# The catalog defaults an operator actually gets from the connect dialog.
LMSTUDIO_URL = "http://host.docker.internal:1234/v1"
MLX_URL = "http://host.docker.internal:2345/v1"
OLLAMA_URL = "http://host.docker.internal:11434/v1"
CLOUD_URL = "https://api.openai.com/v1"


def _provider(kind, *, base_url="", label="p", uid="p1"):
    """A provider row shaped the way the gates read it (no Neo4j needed)."""
    return SimpleNamespace(uid=uid, kind=kind, base_url=base_url, label=label, model="")


# ── resolved_provider_kind ────────────────────────────────────────────────


def test_opencode_on_lmstudio_port_resolves_to_lmstudio():
    assert resolved_provider_kind(_provider("opencode", base_url=LMSTUDIO_URL)) == "lmstudio"


def test_opencode_on_mlx_port_resolves_to_mlx():
    assert resolved_provider_kind(_provider("opencode", base_url=MLX_URL)) == "mlx"


def test_opencode_on_ollama_port_resolves_to_ollama():
    assert resolved_provider_kind(_provider("opencode", base_url=OLLAMA_URL)) == "ollama"


def test_opencode_on_cloud_endpoint_resolves_to_nothing_local():
    """The whole point: the row still says kind="opencode", but the tokens are
    being bought from OpenAI."""
    p = _provider("opencode", base_url=CLOUD_URL)
    assert is_local_provider_kind(p.kind) is True  # the trap this closes
    assert resolved_provider_kind(p) == ""


@pytest.mark.parametrize(
    "base_url",
    [
        "https://openrouter.ai/api/v1",
        "https://my-vps.example.com:1234/v1",  # local PORT, remote host
        "http://8.8.8.8:2345/v1",
    ],
)
def test_off_box_hosts_are_never_local_whatever_the_port(base_url):
    assert resolved_provider_kind(_provider("opencode", base_url=base_url)) == ""


def test_opencode_without_base_url_is_not_local():
    """An unconfigured row proves nothing about where it would run."""
    assert resolved_provider_kind(_provider("opencode", base_url="")) == ""


def test_opencode_with_malformed_authority_is_not_local():
    assert resolved_provider_kind(_provider("opencode", base_url="http://localhost:nope/v1")) == ""


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:9999/v1", "http://127.0.0.1:9999/v1", "http://192.168.1.40:9999/v1"],
)
def test_local_host_on_unknown_port_keeps_its_own_kind(base_url):
    """An operator who moved LM Studio off :1234 has not left their laptop —
    the row stays local, it just cannot be named more precisely."""
    kind = resolved_provider_kind(_provider("opencode", base_url=base_url))
    assert kind == "opencode"
    assert is_local_provider_kind(kind) is True


@pytest.mark.parametrize("kind", ["mlx", "lmstudio", "ollama", "claude_api", "openai_api"])
def test_endpoint_kinds_resolve_to_themselves(kind):
    """Only the agent-shaped CLI kinds have a backend to resolve."""
    assert resolved_provider_kind(_provider(kind, base_url=CLOUD_URL)) == kind


def test_aider_gets_the_same_backend_resolution_as_opencode():
    """aider is the other agent-shaped kind; it must not be an exception that
    quietly keeps the "always local" answer."""
    assert resolved_provider_kind(_provider("aider", base_url=MLX_URL)) == "mlx"
    assert resolved_provider_kind(_provider("aider", base_url=CLOUD_URL)) == ""


# ── the lifecycle write gate ──────────────────────────────────────────────


def test_opencode_on_lmstudio_may_write():
    assert _provider_supports_write(_provider("opencode", base_url=LMSTUDIO_URL)) is True


def test_opencode_on_mlx_may_write():
    assert _provider_supports_write(_provider("opencode", base_url=MLX_URL)) is True


def test_opencode_on_cloud_provider_is_refused():
    assert _provider_supports_write(_provider("opencode", base_url=CLOUD_URL)) is False


def test_opencode_without_base_url_is_refused():
    assert _provider_supports_write(_provider("opencode", base_url="")) is False


def test_opencode_on_ollama_is_refused_even_though_ollama_is_local():
    """The write allowlist is NARROWER than `is_local_provider_kind`.

    Ollama is genuinely local and unmetered, so the wall-ceiling skip applies
    to it — but write autonomy was scoped to LM Studio and MLX specifically.
    Granting it to every local backend because they share the "costs nothing"
    property would extend an unattended edit+commit surface to a pairing
    nobody evaluated.
    """
    provider = _provider("opencode", base_url=OLLAMA_URL)
    assert resolved_provider_kind(provider) == "ollama"
    assert is_local_provider_kind("ollama") is True
    assert _provider_supports_write(provider) is False


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:9999/v1", "http://127.0.0.1:9999/v1", "http://192.168.1.40:9999/v1"],
)
def test_opencode_on_an_unrecognised_local_port_is_refused(base_url):
    """A deliberate false negative: the row is local (so it resolves to
    "opencode", not ""), but the gate cannot tell WHICH backend is behind an
    unrecognised port. Refusing is the safe direction for a decision about
    whether an unattended agent may edit and commit code.
    """
    provider = _provider("opencode", base_url=base_url)
    assert is_local_provider_kind(resolved_provider_kind(provider)) is True
    assert _provider_supports_write(provider) is False


def test_aider_on_lmstudio_is_refused():
    """aider resolves its backend identically, but only opencode is in
    `_LOCAL_ONLY_WRITE_CAPABLE_EXECUTORS` — backend locality is necessary for
    write, never sufficient."""
    assert _provider_supports_write(_provider("aider", base_url=LMSTUDIO_URL)) is False


def test_codex_is_still_refused():
    """`codex exec --json` is a one-shot tracking transport — unchanged."""
    assert _provider_supports_write(_provider("codex_subscription")) is False


def test_claude_code_is_still_allowed():
    assert _provider_supports_write(_provider("claude_subscription")) is True


@pytest.mark.parametrize("kind", ["mlx", "lmstudio", "ollama", "claude_api", "openai_api", "custom"])
def test_internal_llm_kinds_are_refused(kind):
    """internal_llm is HTTP + read tools; a local MLX row is local but has no
    edit surface at all, so "local" alone must not open the write gate."""
    assert _provider_supports_write(_provider(kind, base_url=MLX_URL)) is False


def test_unknown_provider_kind_is_refused():
    assert _provider_supports_write(_provider("something_new")) is False


# ── executor re-resolution for the write playbooks ────────────────────────


def test_write_playbook_pin_reroutes_to_opencode_on_a_local_provider():
    """implement/fix pin Executor.CLAUDE_CODE at their call sites. On an org
    whose only provider is a local opencode row, honouring that pin means the
    claude_code adapter fails with "active provider is not
    kind=claude_subscription" — a guaranteed dead round."""
    chosen = write_executor_for_provider(
        Executor.CLAUDE_CODE,
        _provider("opencode", base_url=MLX_URL),
        ExecutionMode.IMPLEMENT,
    )
    assert chosen is Executor.OPENCODE


def test_write_playbook_pin_is_kept_when_opencode_is_on_a_cloud_endpoint():
    chosen = write_executor_for_provider(
        Executor.CLAUDE_CODE,
        _provider("opencode", base_url=CLOUD_URL),
        ExecutionMode.IMPLEMENT,
    )
    assert chosen is Executor.CLAUDE_CODE


@pytest.mark.parametrize("kind", ["codex_subscription", "openai_api", "mlx"])
def test_write_playbook_pin_is_kept_for_every_non_write_capable_provider(kind):
    """Conservative by design: providers this feature does not cover keep the
    pre-existing "fails loudly without a Claude subscription" behaviour."""
    chosen = write_executor_for_provider(
        Executor.CLAUDE_CODE, _provider(kind, base_url=MLX_URL), ExecutionMode.IMPLEMENT
    )
    assert chosen is Executor.CLAUDE_CODE


def test_analyze_only_runs_are_never_rerouted():
    """A read run pinned to claude_code is a deliberate choice (thread runs,
    review runs) and has nothing to do with write capability."""
    chosen = write_executor_for_provider(
        Executor.CLAUDE_CODE,
        _provider("opencode", base_url=MLX_URL),
        ExecutionMode.ANALYZE_ONLY,
    )
    assert chosen is Executor.CLAUDE_CODE


def test_no_pin_stays_unpinned():
    """`None` means "resolve from the provider" — the caller's normal path."""
    assert (
        write_executor_for_provider(
            None, _provider("opencode", base_url=MLX_URL), ExecutionMode.IMPLEMENT
        )
        is None
    )


def test_matching_pin_is_left_alone():
    chosen = write_executor_for_provider(
        Executor.OPENCODE,
        _provider("opencode", base_url=MLX_URL),
        ExecutionMode.IMPLEMENT,
    )
    assert chosen is Executor.OPENCODE


# ── the adapter's own gate ────────────────────────────────────────────────


def test_adapter_may_write_matches_the_lifecycle_gate():
    adapter = cli_tracking.OpenCodeAdapter()
    assert adapter._may_write(_provider("opencode", base_url=LMSTUDIO_URL)) is True
    assert adapter._may_write(_provider("opencode", base_url=MLX_URL)) is True
    assert adapter._may_write(_provider("opencode", base_url=CLOUD_URL)) is False


def test_codex_adapter_never_writes():
    adapter = cli_tracking.CodexAdapter()
    assert adapter.local_write_capable is False
    assert adapter._may_write(_provider("codex_subscription")) is False


# ── adapter dispatch refusals (async) ─────────────────────────────────────


def _req(**overrides):
    base = dict(
        run_uid="r1",
        scheduled_agent_uid="",
        repository_uid="repo1",
        repository_local_path="/ws",
        intent="implement the ticket",
        mode=ExecutionMode.IMPLEMENT,
    )
    base.update(overrides)
    return DispatchRequest(**base)


def _patch_provider(monkeypatch, provider):
    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)


@pytest.mark.asyncio
async def test_dispatch_refuses_a_write_run_on_a_cloud_opencode_row(monkeypatch):
    """The row could have been repointed at a cloud endpoint after the
    lifecycle gate ran (a write run can sit quota-paused for hours), so the
    adapter re-checks at the moment it would hand over the write sandbox."""
    _patch_provider(monkeypatch, _provider("opencode", base_url=CLOUD_URL, label="my opencode"))

    def _boom(*a, **k):
        raise AssertionError("the CLI must not be invoked for a refused write run")

    monkeypatch.setattr(cli_tracking, "invoke_provider", _boom)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert result.status is RunStatus.FAILED
    assert "implement" in result.error and "local model" in result.error
    assert "my opencode" in result.error


@pytest.mark.asyncio
async def test_dispatch_refuses_a_write_run_on_codex(monkeypatch):
    """Loud refusal, not a silent read-only pass: a write run that returns
    findings instead of commits looks like the agent declined the work, and the
    caller counts the fix round as spent either way."""
    _patch_provider(monkeypatch, _provider("codex_subscription"))

    def _boom(*a, **k):
        raise AssertionError("the CLI must not be invoked for a refused write run")

    monkeypatch.setattr(cli_tracking, "invoke_provider", _boom)

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert result.status is RunStatus.FAILED
    assert "not write-capable" in result.error


@pytest.mark.asyncio
async def test_read_runs_on_a_cloud_opencode_row_still_dispatch(monkeypatch):
    """The gate is about WRITE. Discovery on a cloud-backed opencode row is
    exactly what "bring your own agent" already supported and must not regress.
    """
    _patch_provider(monkeypatch, _provider("opencode", base_url=CLOUD_URL))
    seen = {}

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        seen["system_prompt"] = system_prompt
        seen["instruction"] = instruction
        return SimpleNamespace(
            raw_output='{"tool_calls": [{"tool": "complete_run", "args": {"summary": "s"}}]}',
            stderr="",
            exit_code=0,
            transport="cli",
            error="",
            ok=True,
        )

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _noop())
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)

    result = await cli_tracking.OpenCodeAdapter().dispatch(
        _req(mode=ExecutionMode.ANALYZE_ONLY)
    )

    assert result.status is RunStatus.AWAITING_INPUT
    assert "Investigate only." in seen["instruction"]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_write_run_on_a_local_opencode_row_gets_the_write_contract(monkeypatch):
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    seen = {}

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        seen["system_prompt"] = system_prompt
        seen["instruction"] = instruction
        return SimpleNamespace(
            raw_output='{"tool_calls": [{"tool": "complete_run", "args": {"summary": "s"}}]}',
            stderr="",
            exit_code=0,
            transport="cli",
            error="",
            ok=True,
        )

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _noop())
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert result.status is RunStatus.AWAITING_INPUT
    assert "COMMIT" in seen["instruction"]
    assert "Investigate only." not in seen["instruction"]
    # The agent commits; the platform pushes. Losing this sentence is how an
    # agent ends up with the sandbox's git credentials pointed at origin.
    assert "NEVER push" in seen["system_prompt"]
    assert "Do not edit repository files" not in seen["system_prompt"]


# ── prompts ───────────────────────────────────────────────────────────────


def test_cli_tracking_write_prompt_replaces_the_read_only_rule():
    text = system_prompt("cli_tracking_write")
    assert "Do not edit repository files" not in text
    assert "COMMIT the result inside this working copy" in text


def test_cli_tracking_write_prompt_forbids_pushing():
    """delivery.write_gate is the only thing allowed to push: it validates the
    commits against the denylist and protected branches first. An agent that
    pushed itself would route around the entire gate."""
    text = system_prompt("cli_tracking_write")
    assert "NEVER push" in text
    assert "git push" in text
    assert "validates your commits and pushes with its own credentials" in text


def test_cli_tracking_write_prompt_keeps_the_envelope_contract():
    """Same transport as the read prompt — the run is still parsed by
    `extract_envelope`, so the JSON contract must survive the mode switch."""
    text = system_prompt("cli_tracking_write")
    assert "```json" in text
    assert '"tool_calls"' in text
    assert "complete_run" in text


def test_read_prompt_is_untouched():
    text = system_prompt("cli_tracking")
    assert "Do not edit repository files" in text
    assert "COMMIT" not in text


# ── instruction + continuation ────────────────────────────────────────────


def _instruction_req(**kw):
    return DispatchRequest(
        run_uid="r1",
        scheduled_agent_uid="",
        repository_uid="repo1",
        repository_local_path="/ws",
        intent="do the thing",
        **kw,
    )


def test_write_instruction_asks_for_a_local_commit():
    text = cli_tracking._instruction(_instruction_req(), 600, write_run=True)
    assert "COMMIT" in text
    assert "never push" in text
    assert "Investigate only." not in text


def test_read_instruction_is_unchanged():
    text = cli_tracking._instruction(_instruction_req(), 600)
    assert "Investigate only." in text
    assert "COMMIT" not in text


def test_write_continuation_nudge_targets_the_uncommitted_sandbox():
    """The expensive failure mode of a write run is an agent that edited files
    and stopped: the sandbox is discarded and the round produces nothing."""
    nudge = cli_tracking._CONTINUATION_NUDGE_WRITE
    assert "COMMIT" in nudge
    assert "discarded with the sandbox" in nudge
    assert "never push" in nudge


def test_continuation_prompt_is_shared_by_both_clis():
    """OpenCode has no session resume either, so it uses codex's
    transcript-tail technique; the historical name still resolves."""
    assert cli_tracking.codex_continuation_prompt is cli_tracking.continuation_prompt
    out = cli_tracking.continuation_prompt("KEEP GOING", "…prior transcript…")
    assert "KEEP GOING" in out
    assert "…prior transcript…" in out
    assert "no session resume" in out


def _stub_inv(text, **kw):
    base = dict(
        raw_output=text, stderr="", exit_code=0, transport="cli", error="", ok=True
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_run_plumbing(monkeypatch, *, completed=False):
    """Everything `_run_passes` touches that would otherwise need Neo4j/git."""

    async def _completed(uid):
        return completed

    async def _no_gap(req, envelope):
        return ""

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _completed)
    monkeypatch.setattr(cli_tracking, "area_partition_nudge", _no_gap)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _noop())
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_opencode_now_gets_a_continuation_pass(monkeypatch):
    """OpenCode used to get NO continuation at all: a run that stopped at 80%
    was simply over. It has no session resume, so it uses the same
    transcript-tail re-prompt codex has used since Task 7."""
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    _patch_run_plumbing(monkeypatch)
    calls = []

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        calls.append(instruction)
        if len(calls) == 1:
            return _stub_inv("still working, no envelope here")
        return _stub_inv('{"tool_calls": [{"tool": "complete_run", "args": {"summary": "s"}}]}')

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 2, "an unfinished opencode run must get a second pass"
    assert "still working, no envelope here" in calls[1], "the tail is the context"
    assert result.usage["continuation_pass"] is True
    assert result.usage["continuation_reason"] == "incomplete_run"


@pytest.mark.asyncio
async def test_opencode_write_continuation_uses_the_commit_nudge(monkeypatch):
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    _patch_run_plumbing(monkeypatch)
    calls = []

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        calls.append(instruction)
        return _stub_inv("edited three files")

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)

    await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 2
    assert cli_tracking._CONTINUATION_NUDGE_WRITE in calls[1]


@pytest.mark.asyncio
async def test_a_finished_opencode_run_is_not_re_prompted(monkeypatch):
    """`complete_run` in the first envelope is the authoritative finish signal
    — envelope tool calls execute after this gate, so completed_at is not
    stamped yet and only the parsed envelope can answer."""
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    _patch_run_plumbing(monkeypatch)
    calls = []

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        calls.append(instruction)
        return _stub_inv('{"tool_calls": [{"tool": "complete_run", "args": {"summary": "s"}}]}')

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 1
    assert result.usage["continuation_pass"] is False


@pytest.mark.asyncio
async def test_a_crashed_first_pass_is_not_re_prompted(monkeypatch):
    """Re-prompting a CLI that died wastes the remaining budget on the same
    crash — the pre-existing codex gate, now shared with opencode."""
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    _patch_run_plumbing(monkeypatch)
    calls = []

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        calls.append(instruction)
        return _stub_inv("", error="CLI exited 1", exit_code=1, ok=False)

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 1
    assert result.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_policy_can_still_disable_the_continuation(monkeypatch):
    _patch_provider(monkeypatch, _provider("opencode", base_url=MLX_URL))
    _patch_run_plumbing(monkeypatch)
    calls = []

    async def _invoke(provider, *, system_prompt, instruction, **kw):
        calls.append(instruction)
        return _stub_inv("unfinished")

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)
    policy = SimpleNamespace(
        max_continuation_passes=0,
        max_wall_seconds=None,
        max_tool_turns=None,
        max_files_touched=None,
        warn_at_pct=80,
    )

    await cli_tracking.OpenCodeAdapter().dispatch(_req(policy=policy))

    assert len(calls) == 1


def test_continuation_tail_is_capped():
    """An uncapped tail would re-send a whole long run's transcript as the
    second pass's prompt — on a local model that is the context window."""
    cap = cli_tracking.CONTINUATION_TAIL_CAP
    tail = "OLDEST" + ("." * 50_000) + "NEWEST"
    out = cli_tracking.continuation_prompt("go", tail)
    assert tail[-cap:] in out
    assert "NEWEST" in out, "the tail kept must be the END of the transcript"
    assert "OLDEST" not in out
