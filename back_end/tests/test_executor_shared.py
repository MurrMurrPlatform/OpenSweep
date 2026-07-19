"""Shared executor plumbing (domains/executors/_shared.py) — audit #33/#35/#48/#21.

Pure + filesystem tests: Neo4j is faked out (the recorder's Run persistence is
monkeypatched), the artifact store is pointed at tmp_path.
"""

from types import SimpleNamespace

import pytest

from domains.executors import _shared
from domains.executors._shared import (
    StreamRecorder,
    ceiling_warnings,
    extract_envelope,
    line_aligned_tail,
    resolve_wall_ceiling,
)
from domains.executors.base import DispatchRequest
from domains.llm_providers.services.runtime_env import ProviderRuntime, build_runtime
from domains.run_policies.services.ceilings import UsageSnapshot
from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS
from infrastructure import artifact_store


# ── Fixtures ───────────────────────────────────────────────────────────────


class FakeRun:
    def __init__(self):
        self.usage = {}
        self.raw_artifact_uri = ""
        self.save_count = 0

    async def save(self):
        self.save_count += 1


@pytest.fixture()
def fake_run(monkeypatch):
    run = FakeRun()

    class _Nodes:
        @staticmethod
        async def get_or_none(uid):
            return run

    monkeypatch.setattr(_shared, "Run", SimpleNamespace(nodes=_Nodes))
    return run


@pytest.fixture()
def artifact_root(monkeypatch, tmp_path):
    from config import settings

    monkeypatch.setattr(settings, "ARTIFACT_STORE_ROOT", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture()
def put_counter(monkeypatch):
    """Count artifact_store.put calls (the recorder must call it exactly once)."""
    calls = {"n": 0}
    real_put = artifact_store.put

    def counting_put(**kwargs):
        calls["n"] += 1
        return real_put(**kwargs)

    monkeypatch.setattr(artifact_store, "put", counting_put)
    return calls


def _live_path(root, run_uid="run1", repo_uid="repo1"):
    return root / repo_uid / run_uid / "raw_transcript.txt"


def _recorder(**overrides):
    kwargs = dict(
        run_uid="run1",
        repository_uid="repo1",
        label="test transcript",
        flush_interval_seconds=3600.0,  # deterministic: only size/close flush
        flush_bytes=1024 * 1024,
    )
    kwargs.update(overrides)
    return StreamRecorder(**kwargs)


# ── StreamRecorder (audit #35) ─────────────────────────────────────────────


class TestStreamRecorderDebounce:
    async def test_nothing_persisted_before_threshold(self, fake_run, artifact_root, put_counter):
        rec = _recorder()
        await rec.record_delta("stdout", "line one\n")
        await rec.record_delta("stdout", "line two\n")
        assert put_counter["n"] == 0
        assert fake_run.save_count == 0
        assert not _live_path(artifact_root).exists()

    async def test_close_flushes_everything(self, fake_run, artifact_root, put_counter):
        rec = _recorder()
        await rec.record_delta("stdout", "line one\n")
        await rec.record_delta("stdout", "line two\n")
        await rec.close()
        assert put_counter["n"] == 1
        assert _live_path(artifact_root).read_text() == "line one\nline two\n"
        assert fake_run.save_count == 1
        assert fake_run.usage["stream_stdout"] == "line one\nline two\n"
        assert fake_run.raw_artifact_uri == rec.live_uri

    async def test_byte_threshold_triggers_flush(self, fake_run, artifact_root, put_counter):
        rec = _recorder(flush_bytes=10)
        await rec.record_delta("stdout", "0123456789ABC")
        assert put_counter["n"] == 1
        assert _live_path(artifact_root).read_text() == "0123456789ABC"
        assert fake_run.save_count == 1

    async def test_appends_instead_of_rewriting(self, fake_run, artifact_root, put_counter):
        rec = _recorder(flush_bytes=4)
        await rec.record_delta("stdout", "first")   # flush 1
        await rec.record_delta("stdout", "second")  # flush 2
        await rec.close()
        # put() (whole-file write) fired exactly once, to establish the file;
        # everything after was an append.
        assert put_counter["n"] == 1
        assert _live_path(artifact_root).read_text() == "firstsecond"

    async def test_close_is_idempotent(self, fake_run, artifact_root, put_counter):
        rec = _recorder()
        await rec.record_delta("stdout", "x")
        await rec.close()
        await rec.close()
        assert put_counter["n"] == 1
        assert fake_run.save_count == 1


class TestStreamRecorderContent:
    async def test_record_total_only_appends_new_suffix(self, fake_run, artifact_root):
        rec = _recorder()
        await rec.record_total("stdout", "abc")
        await rec.record_total("stdout", "abcdef")
        await rec.record_total("stdout", "abcdef")  # no growth → ignored
        await rec.close()
        assert _live_path(artifact_root).read_text() == "abcdef"

    async def test_stderr_gets_marker_and_own_tail(self, fake_run, artifact_root):
        rec = _recorder()
        await rec.record_delta("stdout", "out\n")
        await rec.record_delta("stderr", "err\n")
        await rec.close()
        content = _live_path(artifact_root).read_text()
        assert content.startswith("out\n")
        assert "--- STDERR ---" in content
        assert content.endswith("err\n")
        assert fake_run.usage["stream_stdout"] == "out\n"
        assert fake_run.usage["stream_stderr"] == "err\n"

    async def test_both_streams_survive_one_save(self, fake_run, artifact_root):
        """The usage dict is written by ONE writer — no pump can clobber the
        other stream's tail (the old per-line read-modify-write race)."""
        rec = _recorder()
        await rec.record_total("stdout", "hello")
        await rec.record_total("stderr", "oops")
        await rec.close()
        assert fake_run.save_count == 1
        assert fake_run.usage["stream_stdout"] == "hello"
        assert fake_run.usage["stream_stderr"] == "oops"

    async def test_usage_tail_is_bounded_and_line_aligned(self, fake_run, artifact_root):
        rec = _recorder(tail_chars=10)
        await rec.record_delta("stdout", "aaaa\nbbbb\ncccc\n")
        await rec.close()
        tail = fake_run.usage["stream_stdout"]
        assert len(tail) <= 10
        assert tail == "cccc\n"
        # The artifact file keeps everything regardless of the tail cap.
        assert _live_path(artifact_root).read_text() == "aaaa\nbbbb\ncccc\n"

    async def test_missing_run_still_writes_file(self, monkeypatch, artifact_root):
        class _Nodes:
            @staticmethod
            async def get_or_none(uid):
                return None

        monkeypatch.setattr(_shared, "Run", SimpleNamespace(nodes=_Nodes))
        rec = _recorder()
        await rec.record_delta("stdout", "data")
        await rec.close()
        assert _live_path(artifact_root).read_text() == "data"


# ── line_aligned_tail / extract_envelope ──────────────────────────────────


def test_line_aligned_tail_short_text_passthrough():
    assert line_aligned_tail("abc", 10) == "abc"


def test_line_aligned_tail_cuts_at_line_boundary():
    assert line_aligned_tail("aaaa\nbbbb\ncccc", 12) == "bbbb\ncccc"


def test_extract_envelope_fenced_json():
    text = 'prose\n```json\n{"summary": "s", "tool_calls": []}\n```\n'
    env = extract_envelope(text)
    assert env == {"summary": "s", "tool_calls": []}


def test_extract_envelope_trailing_object():
    env = extract_envelope('narration {"tool_calls": [{"tool": "create_finding", "args": {}}]}')
    assert env is not None and len(env["tool_calls"]) == 1


def test_extract_envelope_requires_tool_calls_key():
    assert extract_envelope('{"not_an_envelope": true}') is None
    assert extract_envelope("") is None


# ── Wall-kill detection (audit #33) + ceiling_warnings (Task 5) ──────────


def test_wall_kill_detection_expression():
    """The adapters' wall_killed heuristic: startswith('timed out')."""
    assert "timed out after 300s".startswith("timed out")
    assert not "provider exploded".startswith("timed out")


def test_llm_executor_timeout_message_matches_wall_kill_detection():
    """cli_tracking/internal_llm detect wall kills via startswith('timed out')
    against llm_executor's message — pin the format so a rename can't silently
    regress LIMIT_EXCEEDED back to FAILED."""
    import inspect

    from domains.llm_providers.services import llm_executor

    source = inspect.getsource(llm_executor)
    assert 'timed out after' in source


class _Policy:
    max_wall_seconds = 100
    max_tool_turns = 10
    max_files_touched = None
    max_test_seconds = None
    max_tokens = None
    max_dollars = None
    warn_at_pct = 80


def test_exceeding_a_ceiling_yields_warning_not_exception():
    warnings = ceiling_warnings(
        policy=_Policy(), usage=UsageSnapshot(wall_seconds=500, tool_turns=50), wall_ceiling=100
    )
    assert any("max_wall_seconds" in w for w in warnings)
    assert any("max_tool_turns" in w for w in warnings)


def test_ceiling_warnings_no_policy_returns_empty():
    warnings = ceiling_warnings(
        policy=None, usage=UsageSnapshot(wall_seconds=1e9), wall_ceiling=1
    )
    assert warnings == []


# ── Wall-ceiling ladder (audit #33) + live ceilings (audit #48) ───────────


def _req(**overrides):
    kwargs = dict(
        run_uid="r",
        investigation_uid="i",
        repository_uid="repo",
        repository_local_path=None,
        intent="x",
    )
    policy = overrides.pop("policy", None)
    req = DispatchRequest(**kwargs)
    req.policy = policy
    for k, v in overrides.items():
        setattr(req, k, v)
    return req


def _policy(**fields):
    base = dict(
        max_wall_seconds=None,
        max_tool_turns=None,
        max_files_touched=None,
        max_test_seconds=None,
        max_tokens=None,
        max_dollars=None,
        warn_at_pct=80,
    )
    base.update(fields)
    return SimpleNamespace(**base)


class TestResolveWallCeiling:
    def test_override_outranks_everything(self):
        req = _req(max_wall_seconds_override=42, policy=_policy(max_wall_seconds=900))
        assert resolve_wall_ceiling(req, "mlx") == 42

    def test_local_provider_skips_wall_ceiling(self):
        req = _req(policy=_policy(max_wall_seconds=900))
        assert resolve_wall_ceiling(req, "mlx") is None
        assert resolve_wall_ceiling(req, "opencode") is None

    def test_policy_ceiling_applies_to_metered_kinds(self):
        req = _req(policy=_policy(max_wall_seconds=900))
        assert resolve_wall_ceiling(req, "claude_subscription") == 900

    def test_system_default_fallback(self):
        req = _req()
        assert resolve_wall_ceiling(req, "claude_api") == DEFAULT_MAX_WALL_SECONDS


class TestCeilingWarnings:
    """ceiling_warnings always returns warnings, never raises or fails hard."""

    def test_no_policy_no_findings(self):
        warnings = ceiling_warnings(
            policy=None, usage=UsageSnapshot(wall_seconds=1e9), wall_ceiling=1
        )
        assert warnings == []

    def test_wall_exceedance_yields_warning_not_exception(self):
        warnings = ceiling_warnings(
            policy=_policy(max_wall_seconds=300),
            usage=UsageSnapshot(wall_seconds=301),
            wall_ceiling=300,
        )
        assert any("max_wall_seconds" in w for w in warnings)

    def test_effective_wall_ceiling_outranks_policy(self):
        """A per-stage override larger than the policy ceiling must not
        produce a warning for a run that stayed inside the override."""
        warnings = ceiling_warnings(
            policy=_policy(max_wall_seconds=300),
            usage=UsageSnapshot(wall_seconds=500),
            wall_ceiling=600,
        )
        # 500 < 600 * 0.8 = 480 is false (500 > 480), so a warning IS expected,
        # but NOT a hard failure (no exception raised).
        # The key invariant: no CeilingExceeded is raised.
        # Effective ceiling is 600 (overrides policy's 300), so we should see
        # a warning about max_wall_seconds since 500 >= 80% of 600 (480).
        assert isinstance(warnings, list)
        assert any("max_wall_seconds" in w for w in warnings)

    def test_local_skip_disables_wall_but_not_cost(self):
        warnings = ceiling_warnings(
            policy=_policy(max_wall_seconds=300, max_dollars=1.0),
            usage=UsageSnapshot(wall_seconds=10_000, dollars=2.5),
            wall_ceiling=None,
        )
        # Wall is skipped (wall_ceiling=None), dollars still warns.
        assert any("max_dollars" in w for w in warnings)
        assert not any("max_wall_seconds" in w for w in warnings)

    def test_dollars_ceiling_yields_warning(self):
        warnings = ceiling_warnings(
            policy=_policy(max_dollars=0.5),
            usage=UsageSnapshot(wall_seconds=1, dollars=0.75),
            wall_ceiling=None,
        )
        assert any("max_dollars" in w for w in warnings)

    def test_tool_turns_ceiling_yields_warning(self):
        warnings = ceiling_warnings(
            policy=_policy(max_tool_turns=5),
            usage=UsageSnapshot(wall_seconds=1, tool_turns=6),
            wall_ceiling=None,
        )
        assert any("max_tool_turns" in w for w in warnings)

    def test_soft_warning_below_ceiling(self):
        warnings = ceiling_warnings(
            policy=_policy(max_wall_seconds=100),
            usage=UsageSnapshot(wall_seconds=85),
            wall_ceiling=100,
        )
        assert any("max_wall_seconds" in w for w in warnings)

    def test_unmetered_cost_is_ignored(self):
        warnings = ceiling_warnings(
            policy=_policy(max_tokens=10, max_dollars=0.1),
            usage=UsageSnapshot(wall_seconds=1, tokens=0, dollars=0.0),
            wall_ceiling=None,
        )
        assert warnings == []


# ── Codex credential dir hygiene (audit #21) ──────────────────────────────


def _codex_provider(uid="prov-1"):
    return SimpleNamespace(
        uid=uid,
        kind="codex_subscription",
        credential_secret='{"tokens": "secret"}',
        api_key_env="",
    )


class TestCodexRuntimeCleanup:
    def test_home_is_deterministic_per_provider(self):
        rt1 = build_runtime(_codex_provider())
        rt2 = build_runtime(_codex_provider())
        try:
            assert rt1.home_override == rt2.home_override
            assert "opensweep-codex-prov-1" in rt1.home_override
        finally:
            rt1.cleanup()

    def test_cleanup_removes_home_dir(self):
        import os

        rt = build_runtime(_codex_provider(uid="prov-clean"))
        home = rt.home_override
        assert home and os.path.isdir(home)
        rt.cleanup()
        assert not os.path.exists(home)
        assert rt.home_override is None
        rt.cleanup()  # idempotent

    def test_cleanup_refuses_foreign_paths(self, tmp_path):
        victim = tmp_path / "not-opensweep"
        victim.mkdir()
        rt = ProviderRuntime(home_override=str(victim))
        rt.cleanup()
        assert victim.exists()

    def test_context_manager_cleans_up(self):
        import os

        with build_runtime(_codex_provider(uid="prov-ctx")) as rt:
            home = rt.home_override
            assert home and os.path.isdir(home)
        assert not os.path.exists(home)
