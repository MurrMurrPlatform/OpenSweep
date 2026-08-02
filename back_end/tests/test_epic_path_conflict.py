"""Cross-epic path collision — the pure decision.

m0019 moved parallelism deliberately ACROSS epics ("parallelism belongs across
epics, not within one"), which is the one failure mode nothing guarded: two
approved epics whose fixes land in the same files dispatch two runs on two
branches and collide at merge.
"""

from domains.tickets.services.epics.conflicts import (
    PathClaim,
    conflicting_claim,
    overlap,
)
from domains.tickets.services.epics.loader import normalize_path


def _claim(run_uid, paths, started_at="2026-08-02T10:00:00+00:00", ticket_uid="t"):
    return PathClaim(
        run_uid=run_uid,
        ticket_uid=ticket_uid,
        started_at=started_at,
        paths=frozenset(paths),
    )


def test_no_overlap_admits():
    claims = [_claim("r1", ["a/b.py"])]
    assert conflicting_claim(claims, paths=frozenset({"c/d.py"})) is None


def test_overlap_is_detected():
    claims = [_claim("r1", ["a/b.py", "x.py"])]
    hit = conflicting_claim(claims, paths=frozenset({"x.py"}))
    assert hit is not None and hit.run_uid == "r1"


def test_empty_incoming_paths_never_conflict():
    """A ticket whose findings record no paths has made no prediction. Absence
    of evidence is not evidence of overlap — blocking it would refuse every
    hand-written ticket in the product."""
    claims = [_claim("r1", ["a/b.py"])]
    assert conflicting_claim(claims, paths=frozenset()) is None


def test_a_claim_with_no_paths_is_never_a_blocker():
    """Runs dispatched before the stamp existed carry no claim. They must
    degrade to today's behaviour, not to a false positive."""
    claims = [_claim("r1", [])]
    assert conflicting_claim(claims, paths=frozenset({"a/b.py"})) is None


def test_the_blocker_is_deterministic_across_repeated_calls():
    """A dispatch refused twice must name the SAME run both times, or the 409's
    deep-link jumps between runs on every retry."""
    claims = [
        _claim("r-zzz", ["shared.py"], started_at="2026-08-02T09:00:00+00:00"),
        _claim("r-aaa", ["shared.py"], started_at="2026-08-02T11:00:00+00:00"),
    ]
    first = conflicting_claim(claims, paths=frozenset({"shared.py"}))
    second = conflicting_claim(list(reversed(claims)), paths=frozenset({"shared.py"}))
    assert first.run_uid == second.run_uid == "r-zzz", "oldest claim wins, stably"


def test_equal_timestamps_break_ties_on_uid_not_input_order():
    same = "2026-08-02T09:00:00+00:00"
    claims = [
        _claim("r-b", ["shared.py"], started_at=same),
        _claim("r-a", ["shared.py"], started_at=same),
    ]
    assert conflicting_claim(claims, paths=frozenset({"shared.py"})).run_uid == "r-a"


def test_line_anchors_must_be_normalized_away_before_comparing():
    """The exact bug #39 fixed on the `files` axis, restated for this guard:
    every finding in this repo carries a `:12-34` anchor, so comparing raw
    strings means two tickets editing one file never collide."""
    a = frozenset({normalize_path("back_end/app.py:10-20")})
    b = frozenset({normalize_path("back_end/app.py:50-60")})
    assert a == b == frozenset({"back_end/app.py"})
    assert conflicting_claim([_claim("r1", a)], paths=b) is not None


def test_overlap_evidence_is_sorted_and_capped():
    shared = {f"f{i}.py" for i in range(20)}
    out = overlap(frozenset(shared), frozenset(shared))
    assert len(out) == 10
    assert list(out) == sorted(out)


def test_overlap_of_disjoint_sets_is_empty():
    assert overlap(frozenset({"a"}), frozenset({"b"})) == ()
