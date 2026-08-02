"""The skeptic pass, extended to audit findings.

It already existed and was already good — fail-closed, prompted to refute,
"silence never dismisses a finding". It just could not reach the findings that
need it most: it required a PullRequest and a Verdict, it is off by default,
and its judgment was written where `trust_score` could not read it. Meanwhile
the highest-volume audit configurations are the most recall-tuned —
`deep-issue-hunt` tells the model outright to file plausible high-impact
issues it could not confirm, which is defensible only with a pass like this
downstream.
"""

from types import SimpleNamespace

import pytest

from domains.delivery.services import verification_run_service as vrs


def _finding(uid, *, severity="medium", confidence=0.9, status="open"):
    return SimpleNamespace(
        uid=uid,
        title=f"finding {uid}",
        severity=severity,
        confidence=confidence,
        status=status,
        evidence={},
        affected_paths=[f"src/{uid}.py"],
    )


# ── Which findings are worth the run ─────────────────────────────────────


def test_high_and_critical_findings_are_always_verified():
    picked = vrs.select_findings_to_verify(
        [
            _finding("crit", severity="critical"),
            _finding("high", severity="high"),
            _finding("low", severity="low"),
        ]
    )
    assert {f.uid for f in picked} == {"crit", "high"}


def test_a_low_confidence_finding_is_verified_at_any_severity():
    """The model telling us it is unsure is exactly the signal to check."""
    picked = vrs.select_findings_to_verify(
        [_finding("unsure", severity="low", confidence=0.4)]
    )
    assert [f.uid for f in picked] == ["unsure"]


def test_confident_low_severity_findings_are_left_alone():
    """Verifying everything would roughly double a campaign's token cost for
    little extra signal."""
    assert vrs.select_findings_to_verify([_finding("meh", severity="low")]) == []


def test_already_triaged_findings_are_not_re_verified():
    assert (
        vrs.select_findings_to_verify(
            [_finding("done", severity="critical", status="dismissed")]
        )
        == []
    )


def test_selection_is_severity_ordered_and_capped():
    findings = [_finding(f"f{i}", severity="high") for i in range(40)]
    picked = vrs.select_findings_to_verify(findings)
    assert len(picked) == vrs._MAX_VERIFIED_FINDINGS


def test_the_most_severe_survive_the_cap():
    findings = [_finding(f"low{i}", severity="high") for i in range(30)]
    findings.append(_finding("crit", severity="critical"))
    picked = vrs.select_findings_to_verify(findings)
    assert picked[0].uid == "crit"


# ── The prompt ───────────────────────────────────────────────────────────


def _payload():
    return [
        {
            "finding_uid": "f1",
            "title": "SQL injection in /users",
            "severity": "critical",
            "evidence": {"note": "unparameterized"},
            "affected_paths": ["api/users.py"],
        }
    ]


def test_the_campaign_prompt_keeps_the_skeptic_contract():
    intent = vrs.build_campaign_verification_intent("Weekly audit", "repo1", _payload())
    assert "REFUTE" in intent
    assert "`refuted`" in intent and "`confirmed`" in intent and "`needs-human`" in intent
    # Fail-closed, the same as the PR path.
    assert "silence never dismisses a finding" in intent
    # Prompt-injection framing survives the extraction.
    assert "claims under test" in intent


def test_the_campaign_prompt_drops_the_pr_scaffolding():
    """No PR means no branch to check out and no sha to pin — instructions
    that cannot be followed are worse than absent ones."""
    intent = vrs.build_campaign_verification_intent("Weekly audit", "repo1", _payload())
    assert "git checkout" not in intent
    assert "verdict_uid" not in intent
    assert "pull request" not in intent.lower()
    assert "repository_uid=repo1" in intent


def test_the_pr_prompt_still_pins_the_sha():
    pr = SimpleNamespace(
        repository_uid="repo1", github_number=7, title="PR", head_ref="feat/x"
    )
    verdict = SimpleNamespace(uid="v1", sha="abcdef1234567890")
    intent = vrs.build_verification_intent(pr, verdict, _payload())
    assert "git checkout feat/x" in intent
    assert "abcdef123456" in intent
    assert "verdict_uid=`v1`" in intent


# ── Folding judgments back onto the findings ─────────────────────────────


@pytest.fixture
def fold_seams(monkeypatch):
    captured = {"recorded": [], "dismissed": [], "audits": []}

    async def fake_record(finding_uid, *, result, run_uid, reasoning=""):
        captured["recorded"].append((finding_uid, result))

    async def fake_dismiss(finding_uid, pull_request_uid, run_uid, reasoning):
        captured["dismissed"].append((finding_uid, pull_request_uid))

    async def fake_audit(**kw):
        captured["audits"].append(kw)

    monkeypatch.setattr(vrs, "record_verification_result", fake_record)
    monkeypatch.setattr(vrs, "_dismiss_refuted", fake_dismiss)
    monkeypatch.setattr(vrs, "write_audit", fake_audit)
    return captured


def _run(finding_uids):
    return SimpleNamespace(
        uid="verify-run",
        repository_uid="repo1",
        status="awaiting_input",
        target={"campaign_uid": "c1", "finding_uids": finding_uids},
    )


def _reports(monkeypatch, rows):
    class _Nodes:
        @staticmethod
        async def filter(**_kw):
            return rows

    monkeypatch.setattr(vrs, "FindingVerification", SimpleNamespace(nodes=_Nodes))


async def test_a_refuted_finding_is_dismissed(fold_seams, monkeypatch):
    _reports(
        monkeypatch,
        [SimpleNamespace(finding_uid="f1", result="refuted", reasoning="guard exists")],
    )

    await vrs.finalize_campaign_verification(_run(["f1"]))

    assert fold_seams["dismissed"] == [("f1", "")]


async def test_an_unreported_finding_stays_confirmed(fold_seams, monkeypatch):
    """Fail closed: the run staying silent is not evidence against a finding."""
    _reports(monkeypatch, [])

    await vrs.finalize_campaign_verification(_run(["f1"]))

    assert fold_seams["recorded"] == [("f1", "confirmed")]
    assert fold_seams["dismissed"] == []


async def test_needs_human_is_recorded_as_itself(fold_seams, monkeypatch):
    _reports(
        monkeypatch,
        [SimpleNamespace(finding_uid="f1", result="needs-human", reasoning="unclear")],
    )

    await vrs.finalize_campaign_verification(_run(["f1"]))

    assert fold_seams["recorded"] == [("f1", "needs-human")]


async def test_a_run_with_no_findings_does_nothing(fold_seams, monkeypatch):
    _reports(monkeypatch, [])
    await vrs.finalize_campaign_verification(_run([]))
    assert fold_seams["recorded"] == [] and fold_seams["audits"] == []


# ── Trust actually moves ─────────────────────────────────────────────────


def test_the_verification_verdict_now_reaches_the_trust_score():
    """`_VERIFICATION_DELTA` is documented as the strongest signal either way
    and was unreachable: the only production caller passed no verification
    status, and the outcome lived on a PR Verdict an audit finding never has."""
    from domains.findings.services.trust import trust_score_for

    base = trust_score_for(_finding("f1"))
    refuted = trust_score_for(
        SimpleNamespace(**{**vars(_finding("f1")), "verification_result": "refuted"})
    )
    confirmed = trust_score_for(
        SimpleNamespace(**{**vars(_finding("f1")), "verification_result": "confirmed"})
    )

    assert refuted < base < confirmed


def test_an_explicit_argument_still_wins_over_the_stored_verdict():
    from domains.findings.services.trust import trust_score_for

    finding = SimpleNamespace(
        **{**vars(_finding("f1")), "verification_result": "confirmed"}
    )
    assert trust_score_for(finding, "refuted") < trust_score_for(finding)
