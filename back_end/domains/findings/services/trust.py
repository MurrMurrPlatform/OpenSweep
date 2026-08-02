"""Composite trust score for a Finding — from signals already being stored.

Signal-to-noise is what buyers actually compare in this category, and the
platform was already capturing better false-positive discriminators than most
tools, then sorting by none of them:

  - `source_run_uids` — how many INDEPENDENT runs filed or re-confirmed this
    finding. Agreement between separate runs (different context windows, often
    different models) is the cheapest false-positive signal available, and it
    was surfaced as one line of text.
  - `detected_by_tool` / `detected_by_rule` — a static analyzer confirmed it.
    Categorically stronger than an LLM assertion, and invisible in the UI.
  - `confidence` — the executor's own 0..1 estimate; stored, never ranked on.
  - verification outcome — the skeptic pass's judgment, when one has run.
  - `status` — what a HUMAN eventually decided about the finding. This is the
    only ground truth in the list: a dismissed finding was not real, a fixed
    one was. It was already being stored on every triaged finding and fed
    into nothing, so a repository could dismiss the same false positive ten
    times and the eleventh report would still rank as high as the first.

Kept pure and dependency-free so it is trivially testable and can be computed
in a list query without extra round-trips.
"""

from __future__ import annotations

#: Weight of a single extra corroborating run, and the cap. Two independent
#: runs agreeing is a strong signal; the fifth adds little, and without a cap
#: a long-lived finding on a busy repo would outrank everything on age alone.
_CORROBORATION_STEP = 0.10
_CORROBORATION_CAP = 0.20

#: A static analyzer is deterministic: it either matched or it did not.
_TOOL_CONFIRMED_BONUS = 0.15

#: The skeptic pass is the strongest signal either way — it is a second agent
#: specifically trying to refute the finding with evidence.
#:
#: Keys are the stored vocabulary (delivery.models.VERIFICATION_RESULTS), which
#: is HYPHENATED. This dict used to key "needs_human" with an underscore, so
#: even once a caller supplied a verification status the needs-human case fell
#: through to 0.0 — a silent no-op inside a signal documented as the strongest
#: one available. test_finding_trust pins the two vocabularies together.
_VERIFICATION_DELTA = {
    "confirmed": 0.20,
    "refuted": -0.50,
    "needs-human": -0.05,
}

#: Outcome feedback: what happened to the finding after it was filed. A human
#: verdict outranks every model-produced signal above, so `dismissed` sinks a
#: finding harder than the skeptic pass raises it — an agent claiming 0.95
#: confidence on something a human already threw out is exactly the case the
#: score exists to catch.
#:
#: `wont-fix` and `accepted` are deliberately NOT negative: both mean "real,
#: but we are living with it". Only `dismissed` means "this was not a real
#: problem". `superseded` is bookkeeping (another finding replaced this one),
#: so it demotes without calling the finding wrong.
_OUTCOME_DELTA = {
    "fixed": 0.25,
    "accepted": 0.10,
    "acknowledged": 0.05,
    "wont-fix": 0.0,
    "superseded": -0.15,
    "dismissed": -0.60,
}


def corroboration_count(source_run_uids, source_run_uid: str = "") -> int:
    """Number of distinct runs that filed or re-confirmed this finding.

    `source_run_uids` is lazily backfilled from the singular `source_run_uid`
    on first confirmation, so a finding seen once may have an empty list — it
    still counts as one sighting, not zero.
    """
    uids = {u for u in (source_run_uids or []) if u}
    if source_run_uid:
        uids.add(source_run_uid)
    return max(len(uids), 1)


def trust_score(
    *,
    confidence: float | None = 0.7,
    source_run_uids=None,
    source_run_uid: str = "",
    detected_by_tool: str = "",
    verification_status: str = "",
    status: str = "",
) -> float:
    """A 0..1 trust score. Higher = likelier to be real and worth acting on.

    Starts from the executor's own confidence and adjusts by evidence the
    platform can check independently of what the model claimed, then by the
    outcome — what a human decided about the finding once they looked.
    """
    try:
        score = float(confidence if confidence is not None else 0.7)
    except (TypeError, ValueError):
        score = 0.7

    runs = corroboration_count(source_run_uids, source_run_uid)
    score += min((runs - 1) * _CORROBORATION_STEP, _CORROBORATION_CAP)

    if (detected_by_tool or "").strip():
        score += _TOOL_CONFIRMED_BONUS

    score += _VERIFICATION_DELTA.get((verification_status or "").strip(), 0.0)

    # Clamped BEFORE the outcome is applied, so the model-produced signals
    # cannot bank credit above 1.0 and spend it absorbing the human verdict:
    # three agreeing runs plus a static analyzer plus a skeptic confirmation
    # sum past 1.4, which would leave a dismissed finding still ranking near
    # the top. The human decided last, so the human's delta lands last.
    score = max(0.0, min(1.0, score))
    score += _OUTCOME_DELTA.get((status or "").strip().lower(), 0.0)

    return round(max(0.0, min(1.0, score)), 4)


def trust_score_for(finding, verification_status: str = "") -> float:
    """`trust_score` for a Finding node or any object with the same fields.

    Reads `status` AND the skeptic pass's verdict off the finding rather than
    taking them as arguments, so both reach every existing caller — the DTO
    boundary computes trust for the whole list, and there is nowhere else they
    would be threaded through.

    The verification half used to be unreachable: this parameter existed,
    defaulted to "", and the sole production caller passed nothing. The
    outcome lived on the PR Verdict, which an audit finding does not have.
    `Finding.verification_result` is the denormalised copy that makes
    `_VERIFICATION_DELTA` — documented as the strongest signal either way —
    actually apply. An explicit argument still wins, for callers that have a
    fresher judgment in hand than the one on the node.
    """
    return trust_score(
        confidence=getattr(finding, "confidence", 0.7),
        source_run_uids=getattr(finding, "source_run_uids", None),
        source_run_uid=getattr(finding, "source_run_uid", "") or "",
        detected_by_tool=getattr(finding, "detected_by_tool", "") or "",
        verification_status=(
            verification_status
            or str(getattr(finding, "verification_result", "") or "")
        ),
        status=str(getattr(finding, "status", "") or ""),
    )
