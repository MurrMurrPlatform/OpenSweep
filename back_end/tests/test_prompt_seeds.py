"""Pure-Python tests for the seeded prompt library (defaults + variants).

DB writes are integration territory; here we pin the specs themselves:
every workflow stage has a default, variants reference real stages and
produces, source URLs never collide, and bodies stay compact (context is a
public good — a bloated guidance body crowds out the structural contract
it is appended to).
"""

from domains.agents.services.seed_defaults import (
    _DEFAULTS,
    workflow_source_url,
)
from domains.agents.services.seed_variants import (
    _VARIANTS,
    variant_source_url,
)
from domains.agents.models import PRODUCES
from domains.repositories.services.workflow import STAGES

_EFFORTS = {"light", "normal", "deep"}
_MAX_BODY_CHARS = 3000


def test_defaults_cover_every_stage_exactly():
    # 'analysis' is deliberately default-less: a seeded stage default would
    # silently replace the "deep-scan" agent base (seed_agent_bases.py) —
    # stage pins land in prompt_body, which outranks the instructions layer.
    assert set(_DEFAULTS) == set(STAGES) - {"analysis"}


def test_default_specs_are_well_formed():
    for stage, spec in _DEFAULTS.items():
        assert spec["title"], stage
        assert spec["description"], stage
        assert spec["body"].strip(), stage
        assert len(spec["body"]) <= _MAX_BODY_CHARS, f"{stage}: body too long"
        assert spec["produces"] in PRODUCES, stage
        assert "opensweep-default" in spec["tags"], stage


def test_variant_specs_are_well_formed():
    for slug, spec in _VARIANTS.items():
        assert spec["title"], slug
        assert spec["description"], slug
        assert spec["body"].strip(), slug
        assert len(spec["body"]) <= _MAX_BODY_CHARS, f"{slug}: body too long"
        assert spec["stage"] in STAGES, slug
        assert spec["produces"] in PRODUCES, slug
        assert spec["default_effort"] in _EFFORTS, slug
        assert "opensweep-variant" in spec["tags"], slug
        # Stage tag lets the UI group variants next to their stage default.
        assert spec["stage"] in spec["tags"], slug


def test_variant_stages_offer_a_depth_choice():
    """The ask and review stages ship both a deep and a quick variant, so the
    recall/precision dial is always available where it matters most."""
    by_stage: dict[str, set[str]] = {}
    for spec in _VARIANTS.values():
        by_stage.setdefault(spec["stage"], set()).add(spec["default_effort"])
    for stage in ("ask", "review"):
        assert {"deep", "light"} <= by_stage.get(stage, set()), stage


def test_source_urls_are_unique_and_disjoint():
    default_urls = {workflow_source_url(stage) for stage in _DEFAULTS}
    variant_urls = [variant_source_url(slug) for slug in _VARIANTS]
    assert len(variant_urls) == len(set(variant_urls))
    assert not default_urls & set(variant_urls)


def test_review_quick_gate_carries_the_finding_budget():
    """§B: the quick depth block promises "at most 5 findings" — the variant
    guidance must agree with the structural intent, not fight it."""
    assert "at most 5" in _VARIANTS["review-quick-gate"]["body"].lower()


def test_review_adversarial_delegates_lenses_to_subagents():
    assert "subagent" in _VARIANTS["review-adversarial"]["body"].lower()


def test_news_scout_files_news_items_and_never_findings():
    """The news scout writes to the news board only; converting news into
    findings is a human-only action, so the body must forbid create_finding."""
    body = _VARIANTS["news-scout"]["body"]
    assert "create_news_item" in body
    assert "NEVER call create_finding" in body


def test_feature_ideas_hunt_files_feature_ideas_only():
    body = _VARIANTS["feature-ideas-hunt"]["body"]
    assert "kind=feature-idea" in body
    assert "Do NOT file defects" in body
