"""Trust ranking, and the dedupe key that used to fork on a file move.

The platform already stored better false-positive signals than it ranked on:
cross-run corroboration (`source_run_uids`), static-analyzer confirmation
(`detected_by_tool`), the executor's `confidence`, and the skeptic pass's
verdict. None of them ordered the findings list.

The dedupe key made the corroboration signal worse: it hashed the full top
path, so relocating a file produced a NEW key and the same issue was filed
again as a second Finding — splitting the very count that measures agreement.
"""

import pytest

from domains.findings.services.dedupe import build_dedupe_key, path_basename
from domains.findings.services.trust import corroboration_count, trust_score

# ── Trust ────────────────────────────────────────────────────────────────


def test_defaults_to_the_executor_confidence():
    assert trust_score(confidence=0.7) == 0.7


def test_corroboration_raises_trust_and_is_capped():
    one = trust_score(confidence=0.5, source_run_uids=["r1"])
    two = trust_score(confidence=0.5, source_run_uids=["r1", "r2"])
    five = trust_score(confidence=0.5, source_run_uids=["r1", "r2", "r3", "r4", "r5"])

    assert two > one
    assert five > two
    # Capped, so a long-lived finding cannot outrank everything on age alone.
    assert five == trust_score(
        confidence=0.5, source_run_uids=[f"r{i}" for i in range(20)]
    )


def test_static_analyzer_confirmation_beats_a_bare_assertion():
    llm_only = trust_score(confidence=0.6)
    tool_confirmed = trust_score(confidence=0.6, detected_by_tool="ruff")
    assert tool_confirmed > llm_only


def test_refuted_by_the_skeptic_pass_sinks_it():
    assert trust_score(confidence=0.9, verification_status="refuted") < 0.5


def test_confirmed_by_the_skeptic_pass_raises_it():
    assert trust_score(confidence=0.6, verification_status="confirmed") > 0.6


def test_score_stays_within_bounds():
    hot = trust_score(
        confidence=1.0,
        source_run_uids=["a", "b", "c"],
        detected_by_tool="semgrep",
        verification_status="confirmed",
    )
    cold = trust_score(confidence=0.0, verification_status="refuted")
    assert 0.0 <= cold <= hot <= 1.0


@pytest.mark.parametrize(
    "uids,single,expected",
    [
        ([], "", 1),          # never zero — one sighting is one sighting
        ([], "r1", 1),        # not yet backfilled from the singular field
        (["r1"], "r1", 1),    # backfilled: not double-counted
        (["r1", "r2"], "r1", 2),
        (["r1", "r1"], "", 1),  # duplicates are not independent runs
    ],
)
def test_corroboration_count(uids, single, expected):
    assert corroboration_count(uids, single) == expected


def test_ranking_puts_corroborated_tool_findings_on_top():
    """The whole point: the list order should reflect credibility."""
    speculative = trust_score(confidence=0.8)
    corroborated = trust_score(
        confidence=0.6, source_run_uids=["r1", "r2"], detected_by_tool="ruff"
    )
    assert corroborated > speculative


# ── Dedupe key ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("src/auth/session.py", "session.py"),
        ("src/auth/session.py:42", "session.py"),
        ("session.py", "session.py"),
        ("SRC/Auth/Session.py", "session.py"),
        ("", ""),
    ],
)
def test_path_basename(raw, expected):
    assert path_basename(raw) == expected


def test_key_survives_a_file_move():
    """The headline regression: moving a file used to fork the finding."""
    before = build_dedupe_key(
        repository_uid="repo-1",
        title="Missing timeout on the outbound HTTP call",
        top_path="src/auth/session.py",
    )
    after = build_dedupe_key(
        repository_uid="repo-1",
        title="Missing timeout on the outbound HTTP call",
        top_path="src/core/session.py",
    )
    assert before == after


def test_key_still_separates_different_files():
    """The fix must not collapse genuinely distinct findings."""
    a = build_dedupe_key(
        repository_uid="repo-1", title="Missing timeout", top_path="src/a.py"
    )
    b = build_dedupe_key(
        repository_uid="repo-1", title="Missing timeout", top_path="src/b.py"
    )
    assert a != b


def test_key_still_separates_repositories():
    a = build_dedupe_key(repository_uid="repo-1", title="X", top_path="a.py")
    b = build_dedupe_key(repository_uid="repo-2", title="X", top_path="a.py")
    assert a != b
