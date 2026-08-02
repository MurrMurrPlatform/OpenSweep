"""Stuck-run repair — pure decision logic for the two reconcile paths:
liveness/wall-ceiling staleness (beat tick + lazy) and process-restart
orphaning (startup sweeps)."""

from datetime import datetime, timedelta, timezone

from domains.runs.services.run_reconciliation import is_orphan, stale_reason

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


# --- stale_reason -----------------------------------------------------------


def test_live_run_is_not_stale():
    assert (
        stale_reason(
            now=NOW,
            started=_ago(3600),
            last_seen=_ago(30),
            wall_ceiling_seconds=None,
            liveness_timeout_seconds=900,
            grace_seconds=90,
        )
        is None
    )


def test_silent_run_is_stale_even_for_local_providers():
    # Local providers have no wall ceiling (wall_ceiling_seconds=None) — the
    # old reconciler skipped them entirely and they stayed running forever.
    reason = stale_reason(
        now=NOW,
        started=_ago(3600),
        last_seen=_ago(901),
        wall_ceiling_seconds=None,
        liveness_timeout_seconds=900,
        grace_seconds=90,
    )
    assert reason is not None
    assert "without executor activity" in reason


def test_metered_run_past_ceiling_is_stale_despite_recent_activity():
    reason = stale_reason(
        now=NOW,
        started=_ago(600 + 91),
        last_seen=_ago(10),
        wall_ceiling_seconds=600,
        liveness_timeout_seconds=900,
        grace_seconds=90,
    )
    assert reason is not None
    assert "wall-time ceiling" in reason


def test_metered_run_within_ceiling_and_alive_is_kept():
    assert (
        stale_reason(
            now=NOW,
            started=_ago(600),
            last_seen=_ago(10),
            wall_ceiling_seconds=600,
            liveness_timeout_seconds=900,
            grace_seconds=90,
        )
        is None
    )


# --- is_orphan --------------------------------------------------------------


def test_own_role_runs_are_orphaned_immediately_even_when_recently_active():
    # The dispatch task died with the process — recent transcript activity
    # written just before the crash must not defer the repair.
    assert is_orphan(
        owner="backend",
        role="backend",
        last_seen=_ago(5),
        now=NOW,
        recent_activity_grace_seconds=120,
    )


def test_other_role_runs_are_never_orphaned():
    assert not is_orphan(
        owner="worker",
        role="backend",
        last_seen=_ago(100_000),
        now=NOW,
        recent_activity_grace_seconds=120,
    )


def test_unstamped_run_with_recent_activity_is_kept():
    # Deploy window: a pre-stamp run may be owned by the OTHER role and still
    # be alive — only its transcript going quiet proves it dead.
    assert not is_orphan(
        owner="",
        role="backend",
        last_seen=_ago(30),
        now=NOW,
        recent_activity_grace_seconds=120,
    )


def test_unstamped_quiet_or_signalless_run_is_orphaned():
    assert is_orphan(
        owner="",
        role="backend",
        last_seen=_ago(121),
        now=NOW,
        recent_activity_grace_seconds=120,
    )
    assert is_orphan(
        owner="",
        role="backend",
        last_seen=None,
        now=NOW,
        recent_activity_grace_seconds=120,
    )


# --- read paths must not sweep the graph ------------------------------------


def test_read_endpoints_do_not_sweep():
    """The graph-wide sweep belongs to the Celery beat tick.

    reconcile_stale_runs() scans every queued/running Run across every
    tenant. It used to be called inline from the runs list, the run detail
    and the scheduled-agent runs list, so one page load cost a full
    cross-tenant scan. Request handlers repair only rows they already hold
    (reconcile_runs); nothing on a request path may call the sweep again.
    """
    import ast
    import pathlib

    api = pathlib.Path(__file__).resolve().parents[1] / "api"
    offenders = [
        f"{path.relative_to(api.parent)}:{node.lineno}"
        for path in api.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Name) and node.id == "reconcile_stale_runs"
    ]
    assert offenders == [], f"graph-wide run sweep on a request path: {offenders}"
