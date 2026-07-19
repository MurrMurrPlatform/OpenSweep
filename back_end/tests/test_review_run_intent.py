"""Review-run intent contract — the prompt must bind the verdict to the SHA."""

from domains.delivery.models import PullRequest
from domains.delivery.services.review_run_service import build_review_intent

HEAD = "c0ffee1234deadbeefc0ffee1234deadbeefc0ff"


def _pr() -> PullRequest:
    return PullRequest(
        uid="pr1",
        repository_uid="repo1",
        github_number=72,
        pr_key="repo1:72",
        title="Event messaging",
        head_sha=HEAD,
        head_ref="feat/event-messaging",
        base_ref="main",
    )


def test_intent_pins_the_exact_head_sha():
    intent = build_review_intent(_pr(), {"default": "high"})
    assert HEAD in intent
    assert f"`{HEAD[:12]}`" in intent  # rev-parse confirmation step


def test_intent_requires_needs_human_fallback_not_guessing():
    intent = build_review_intent(_pr(), {"default": "high"})
    assert "needs_human" in intent
    assert "never guess" in intent.lower()


def test_intent_mandates_verdict_and_ledger_tools():
    intent = build_review_intent(_pr(), {"default": "high"})
    for tool in (
        "opensweep_platform_submit_verdict",
        "opensweep_platform_create_finding",
        "opensweep_platform_bind_finding_to_pr",
        "opensweep_platform_list_pr_resolutions",
        "opensweep_platform_verify_resolution",
    ):
        assert tool in intent, f"intent must reference {tool}"


def test_intent_is_read_only_and_policy_aware():
    intent = build_review_intent(_pr(), {"default": "high", "per_concern": {"security": "medium"}})
    assert "read-only" in intent.lower()
    assert "security" in intent  # policy embedded so blocking counts match the predicate
    assert "git diff main...feat/event-messaging" in intent


# ── Depth dial (§B) ──────────────────────────────────────────────────────────


def test_quick_depth_caps_findings_and_allows_empty_review():
    intent = build_review_intent(_pr(), {"default": "high"}, depth="quick")
    assert "at most 5 findings" in intent.lower()
    assert "empty review is a valid outcome" in intent.lower()


def test_short_depth_is_identical_to_quick():
    """Legacy 'quick' and canonical 'short' must produce the same precision stance."""
    quick = build_review_intent(_pr(), {"default": "high"}, depth="quick")
    short = build_review_intent(_pr(), {"default": "high"}, depth="short")
    assert quick == short
    assert "at most 5 findings" in short.lower()
    assert "empty review is a valid outcome" in short.lower()


def test_deep_depth_is_exhaustive_with_subagent_lenses():
    intent = build_review_intent(_pr(), {"default": "high"}, depth="deep")
    assert "subagent" in intent.lower()
    assert "no hard finding cap" in intent.lower()


def test_max_findings_knob_overrides_every_depth():
    quick = build_review_intent(_pr(), {}, depth="quick", max_findings=12)
    assert "at most 12 findings" in quick.lower()
    assert "at most 5" not in quick.lower()
    normal = build_review_intent(_pr(), {}, depth="normal", max_findings=3)
    assert "at most 3 findings" in normal.lower()
    deep = build_review_intent(_pr(), {}, depth="deep", max_findings=8)
    assert "at most 8 findings" in deep.lower()
    assert "subagent" in deep.lower()  # lenses survive the cap


def test_normal_depth_has_no_cap_and_no_subagents():
    intent = build_review_intent(_pr(), {"default": "high"}, depth="normal")
    assert "at most 5" not in intent.lower()
    assert "subagent" not in intent.lower()
    # Unknown depth values fall back to the normal block, never crash.
    assert build_review_intent(_pr(), {}, depth="bogus") == build_review_intent(
        _pr(), {}, depth="normal"
    )


# ── Incremental re-review (§D) ───────────────────────────────────────────────

PRIOR = "beef00beef00beef00beef00beef00beef00beef"


def test_incremental_scope_diffs_from_the_prior_verdict_sha():
    intent = build_review_intent(
        _pr(), {"default": "high"}, prior_verdict_sha=PRIOR
    )
    assert f"git diff {PRIOR}...feat/event-messaging" in intent
    assert f"git cat-file -e {PRIOR}" in intent  # shallow-clone fallback probe
    # Full-scope fallback stays available when the commit is absent.
    assert "git diff main...feat/event-messaging" in intent


def test_incremental_scope_rechecks_every_open_resolution():
    intent = build_review_intent(
        _pr(), {"default": "high"}, prior_verdict_sha=PRIOR
    )
    assert "EVERY resolution" in intent


def test_full_review_intent_has_no_incremental_language():
    intent = build_review_intent(_pr(), {"default": "high"})
    assert "Incremental" not in intent
    assert "cat-file" not in intent
