"""Write-gate safety rules — pure parts, no git required (§6 Phase 3)."""

import asyncio
from types import SimpleNamespace

import pytest

from domains.delivery.models import DEFAULT_PATH_DENYLIST
from domains.delivery.services import write_gate
from domains.delivery.services.write_gate import (
    WriteGateResult,
    _reportable_argv,
    denylist_violations,
    effective_denylist,
    evaluate_changes,
    fix_rounds_exhausted,
    is_protected_branch,
)

# ── Denylist matching ────────────────────────────────────────────────────────


def test_default_denylist_blocks_the_sensitive_classes():
    changed = [
        "src/auth/session.py",
        "payments/stripe.py",
        "back_end/migrations/0007_add.py",
        "app/.env.production",
        "config/secrets.yaml",
        "deployment/Caddyfile",
    ]
    violations = denylist_violations(changed, DEFAULT_PATH_DENYLIST)
    assert len(violations) == len(changed), violations


def test_denylist_allows_ordinary_source_paths():
    changed = [
        "src/utils/format.py",
        "front_end/components/Button.vue",
        "README.md",
        "tests/test_format.py",
        "src/author_profile.py",  # "auth" only matches as a directory segment
    ]
    assert denylist_violations(changed, DEFAULT_PATH_DENYLIST) == []


def test_denylist_matches_nested_and_root_segments():
    assert denylist_violations(["auth/login.py"], DEFAULT_PATH_DENYLIST)
    assert denylist_violations(["a/b/auth/login.py"], DEFAULT_PATH_DENYLIST)
    assert denylist_violations(["payment/checkout.py"], DEFAULT_PATH_DENYLIST)
    assert denylist_violations(["migration/0001.sql"], DEFAULT_PATH_DENYLIST)


def test_invalid_denylist_pattern_fails_closed():
    violations = denylist_violations(["anything.py"], ["([unclosed"])
    assert violations and "invalid denylist pattern" in violations[0]


def test_effective_denylist_none_means_defaults_empty_means_opt_out():
    assert effective_denylist(SimpleNamespace(path_denylist=None)) == list(DEFAULT_PATH_DENYLIST)
    assert effective_denylist(SimpleNamespace(path_denylist=[])) == []
    assert effective_denylist(SimpleNamespace(path_denylist=["foo"])) == ["foo"]


# ── Protected branches ───────────────────────────────────────────────────────


def test_protected_branch_names_and_default_branch():
    assert is_protected_branch("main")
    assert is_protected_branch("master")
    assert is_protected_branch("develop")
    assert is_protected_branch("trunk", default_branch="trunk")
    assert not is_protected_branch("opensweep/ab12cd34-fix-the-thing")
    assert not is_protected_branch("trunk", default_branch="main")


def test_empty_or_detached_branch_is_treated_as_protected():
    assert is_protected_branch("")
    assert is_protected_branch("  ")


# ── Gate decision core ───────────────────────────────────────────────────────


def test_evaluate_changes_ok_path():
    result = evaluate_changes(
        work_branch="opensweep/ab12cd34-add-endpoint",
        changed_paths=["src/api/routes.py", "tests/test_routes.py"],
        commits=2,
        denylist=DEFAULT_PATH_DENYLIST,
    )
    assert isinstance(result, WriteGateResult)
    assert result.ok
    assert result.violations == []
    assert result.commits == 2


def test_evaluate_changes_zero_commits_is_a_violation():
    result = evaluate_changes(
        work_branch="opensweep/ab12cd34-x", changed_paths=[], commits=0, denylist=[]
    )
    assert not result.ok
    assert any("no commits" in v for v in result.violations)


def test_evaluate_changes_protected_branch_is_a_violation():
    result = evaluate_changes(
        work_branch="main", changed_paths=["src/x.py"], commits=1, denylist=[]
    )
    assert not result.ok
    assert any("protected branch" in v for v in result.violations)


def test_evaluate_changes_denylisted_path_is_a_violation():
    result = evaluate_changes(
        work_branch="opensweep/ab12cd34-x",
        changed_paths=["src/x.py", "auth/tokens.py"],
        commits=1,
        denylist=DEFAULT_PATH_DENYLIST,
    )
    assert not result.ok
    assert any("auth/tokens.py" in v for v in result.violations)
    # violations accumulate — they never mask each other
    result2 = evaluate_changes(
        work_branch="main", changed_paths=["auth/tokens.py"], commits=0,
        denylist=DEFAULT_PATH_DENYLIST,
    )
    assert len(result2.violations) == 3


def test_evaluate_changes_non_opensweep_branch_is_a_violation():
    # A non-protected branch that isn't opensweep/* must not be pushed to.
    result = evaluate_changes(
        work_branch="feature/sneaky", changed_paths=["src/x.py"], commits=1, denylist=[]
    )
    assert not result.ok
    assert any("opensweep/*" in v for v in result.violations)


def test_evaluate_changes_detached_head_is_a_violation():
    # rev-parse --abbrev-ref on a detached HEAD yields the literal "HEAD".
    result = evaluate_changes(
        work_branch="HEAD", changed_paths=["src/x.py"], commits=1, denylist=[]
    )
    assert not result.ok


def test_evaluate_changes_carries_the_validated_branch():
    # Callers must push result.work_branch, so it has to round-trip.
    result = evaluate_changes(
        work_branch="opensweep/ab12cd34-x", changed_paths=["src/x.py"], commits=1, denylist=[]
    )
    assert result.ok
    assert result.work_branch == "opensweep/ab12cd34-x"


# ── Fix-round bound (§6: bounded auto-fix loop) ──────────────────────────────


def test_fix_rounds_exhausted_boundary():
    assert not fix_rounds_exhausted(0, 2)
    assert not fix_rounds_exhausted(1, 2)
    assert fix_rounds_exhausted(2, 2)
    assert fix_rounds_exhausted(3, 2)
    # max 0 = auto-fix disabled entirely
    assert fix_rounds_exhausted(0, 0)


def test_git_auth_header_is_basic_x_access_token():
    """GitHub git endpoints 401 on `bearer` for installation tokens; the
    documented scheme is basic with the x-access-token username."""
    import base64

    from infrastructure.git_auth import git_auth_extraheader

    header = git_auth_extraheader("ghs_sekret")
    assert header.startswith("http.extraHeader=AUTHORIZATION: basic ")
    b64 = header.rsplit(" ", 1)[1]
    assert base64.b64decode(b64).decode() == "x-access-token:ghs_sekret"
    assert "bearer" not in header


# ── Failure-message argv redaction ───────────────────────────────────────────


def test_auth_flag_and_value_are_dropped_together():
    """Stripping only the `http.extraHeader=…` value left its `-c` behind, so a
    failed push reported `git -c push origin <branch>` — which reads as a
    malformed command and sends the reader hunting for a bug in the argv
    instead of at the 403 underneath."""
    argv = ("-c", "http.extraHeader=AUTHORIZATION: basic abc", "push", "origin", "br")
    assert _reportable_argv(argv) == ["push", "origin", "br"]


def test_unrelated_config_flags_survive():
    argv = ("-c", "user.name=OpenSweep", "commit", "-m", "x")
    assert _reportable_argv(argv) == ["-c", "user.name=OpenSweep", "commit", "-m", "x"]


def test_trailing_dash_c_does_not_crash():
    assert _reportable_argv(("push", "-c")) == ["push", "-c"]


# ── Subprocess timeout (a stalled push/fetch must not hang the event loop) ──


@pytest.mark.asyncio
async def test_git_timeout_kills_process_and_raises(monkeypatch):
    killed = {"kill": False, "wait": False}

    class _HangingProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)  # would hang forever without the timeout
            return b"", b""  # pragma: no cover — never reached

        def kill(self):
            killed["kill"] = True

        async def wait(self):
            killed["wait"] = True
            return -9

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(write_gate, "GIT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError, match="timed out"):
        await write_gate._git("/tmp/whatever", "push", "origin", "opensweep/x")

    assert killed["kill"] is True
    assert killed["wait"] is True
