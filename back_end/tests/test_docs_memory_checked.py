"""Pure-Python tests for the KNOWLEDGE_V3 primitives.

DB-bound flows (CRUD, accept/reject, stamping) are integration-tested;
here we pin the pure logic: slug normalization, memory fingerprint dedup
inputs, staleness comparison, and checked outcome mapping.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import domains.checked.services.checked_service as checked_service
from domains.checked.services.checked_service import (
    _coverage_fields,
    _outcome_for_run,
    stamps_for_paths,
)
from domains.docs.services.doc_service import normalize_slug
from domains.memory.services.memory_service import _fingerprint, _possibly_stale


def test_normalize_slug_kebab_cases_and_caps_length():
    assert normalize_slug("Feature: Login with Email!") == "feature-login-with-email"
    assert normalize_slug("  Conventions  ") == "conventions"
    assert normalize_slug("") == ""
    assert len(normalize_slug("x" * 300)) <= 120


def test_normalize_slug_preserves_folders():
    assert normalize_slug("Backend/Queue Workers") == "backend/queue-workers"
    assert normalize_slug("backend//queue-workers/") == "backend/queue-workers"
    assert normalize_slug("/deployment/Terraform!") == "deployment/terraform"


def test_fingerprint_ignores_case_and_whitespace_of_title():
    a = _fingerprint("Flaky Redis fixture", "body text")
    b = _fingerprint("  flaky redis fixture ", "body text")
    assert a == b
    assert a != _fingerprint("flaky redis fixture", "different body")


def test_possibly_stale_compares_anchor_change_to_update():
    now = datetime.now(UTC)
    fresh = SimpleNamespace(anchor_uid="n1", updated_at=now)
    stale = SimpleNamespace(anchor_uid="n1", updated_at=now - timedelta(days=2))
    unanchored = SimpleNamespace(anchor_uid="", updated_at=now - timedelta(days=2))
    change_times = {"n1": now - timedelta(days=1)}
    assert _possibly_stale(fresh, change_times) is False
    assert _possibly_stale(stale, change_times) is True
    assert _possibly_stale(unanchored, change_times) is False
    assert _possibly_stale(stale, {}) is False


def test_checked_outcome_collapses_failures():
    assert _outcome_for_run(status="completed", findings_count=0) == "clean"
    assert _outcome_for_run(status="completed", findings_count=3) == "findings"
    for status in ("failed", "cancelled", "limit_exceeded", "queued", ""):
        assert _outcome_for_run(status=status, findings_count=1) == "failed"


def test_checked_outcome_awaiting_input_counts_as_success():
    # V3 runs land in awaiting_input after a successful turn; the stamp is
    # written by the per-turn hook, so treat it like completion.
    assert _outcome_for_run(status="awaiting_input", findings_count=0) == "clean"
    assert _outcome_for_run(status="awaiting_input", findings_count=2) == "findings"


def test_checked_stamp_carries_agent_reported_coverage():
    # complete_run stored the coverage contract on usage["coverage"]; the
    # stamp must carry it so freshness reflects what was ACTUALLY examined.
    verdicts = [{"lens": "bugs", "verdict": "checked-clean"}]
    covered, skipped, lenses = _coverage_fields(
        usage={
            "coverage": {
                "covered_paths": ["src/a.py"],
                "skipped_paths": ["src/vendored/"],
                "lens_verdicts": verdicts,
            }
        },
        target={"paths": ["src/"]},
    )
    assert covered == ["src/a.py"]  # agent's report wins over the target
    assert skipped == ["src/vendored/"]
    assert lenses == verdicts


def _stamp(uid, covered, *, repo="r1", days_ago=0):
    return SimpleNamespace(
        uid=uid,
        repository_uid=repo,
        run_uid=f"run-{uid}",
        outcome="clean",
        checked_at=datetime.now(UTC) - timedelta(days=days_ago),
        covered_paths=covered,
    )


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)


async def test_stamps_for_paths_matches_on_slash_boundaries(monkeypatch):
    stamps = [
        _stamp("under", ["src/api/a.py"], days_ago=2),  # under the scope
        _stamp("above", ["src"], days_ago=1),  # scope under the covered path
        _stamp("exact", ["src/api"], days_ago=3),
        _stamp("sibling", ["src/api-v2/b.py"]),  # "/"-boundary: no match
        _stamp("other-repo", ["src/api/a.py"], repo="r2"),
    ]
    monkeypatch.setattr(
        checked_service, "Checked", SimpleNamespace(nodes=_Nodes(stamps))
    )
    out = await stamps_for_paths("r1", ["src/api"])
    # Newest first; the boundary rule keeps src/api-v2 (and r2) out.
    assert [c.uid for c in out] == ["above", "under", "exact"]


async def test_stamps_for_paths_limits_and_handles_empty_inputs(monkeypatch):
    stamps = [_stamp(f"s{i}", ["src/x.py"], days_ago=i) for i in range(15)]
    monkeypatch.setattr(
        checked_service, "Checked", SimpleNamespace(nodes=_Nodes(stamps))
    )
    out = await stamps_for_paths("r1", ["src"], limit=10)
    assert len(out) == 10
    assert [c.uid for c in out] == [f"s{i}" for i in range(10)]  # newest first
    assert await stamps_for_paths("r1", []) == []


def test_checked_stamp_falls_back_to_target_paths_without_a_report():
    # Agent didn't report coverage — the dispatched scope is the best
    # available claim of what it looked at; lens_verdicts default empty.
    covered, skipped, lenses = _coverage_fields(
        usage={}, target={"paths": ["src/", "tests/"]}
    )
    assert covered == ["src/", "tests/"]
    assert skipped == []
    assert lenses == []
    # Malformed inputs degrade to empty, never raise.
    covered, _, lenses = _coverage_fields(
        usage={"coverage": {"lens_verdicts": ["not-a-dict"]}},
        target={"paths": "not-a-list"},
    )
    assert covered == [] and lenses == []
