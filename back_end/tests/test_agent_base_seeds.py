"""Pure-Python pins for the seeded system-agent bases
(opensweep://agent/<key>) — the task-instructions layer org overrides
apply to. DB writes are integration territory; here we pin the specs."""

from domains.agents.models import PRODUCES
from domains.agents.services.registry import AGENT_KEYS, agent_source_url
from domains.agents.services.seed_agent_bases import (
    _AGENT_BASES,
    agent_base_fallback,
)
from domains.agents.services.seed_defaults import _DEFAULTS, workflow_source_url
from domains.agents.services.seed_variants import _VARIANTS, variant_source_url


def test_bases_cover_every_agent_key_exactly():
    assert set(_AGENT_BASES) == set(AGENT_KEYS)
    assert len(AGENT_KEYS) == 12


def test_base_specs_are_well_formed():
    for key, spec in _AGENT_BASES.items():
        assert spec["title"], key
        assert spec["description"], key
        assert spec["body"].strip(), key
        assert spec["produces"] in PRODUCES, key
        assert "opensweep-agent-base" in spec["tags"], key
        assert key in spec["tags"], key


def test_source_urls_are_opensweep_agent_uris_and_disjoint():
    base_urls = {agent_source_url(key) for key in _AGENT_BASES}
    assert base_urls == {f"opensweep://agent/{key}" for key in _AGENT_BASES}
    workflow_urls = {workflow_source_url(s) for s in _DEFAULTS}
    variant_urls = {variant_source_url(s) for s in _VARIANTS}
    assert not base_urls & workflow_urls
    assert not base_urls & variant_urls


def test_in_code_fallback_matches_the_shipped_body():
    """The last-resort fallback (used when the seeded row is deleted or
    disabled) is the same in-code string the seeder ships."""
    for key in AGENT_KEYS:
        assert agent_base_fallback(key) == _AGENT_BASES[key]["body"]
    assert agent_base_fallback("nope") == ""


def test_bases_carry_task_instructions_not_structure():
    """Structural anchors (tool call sequences, shas, branch names, verdict
    plumbing) stay in code — the editable bodies must not hardcode them."""
    for key, spec in _AGENT_BASES.items():
        body = spec["body"]
        assert "git checkout" not in body, key
        assert "opensweep_platform_submit_verdict" not in body, key
        assert "{" not in body, f"{key}: no format placeholders in seeded bodies"


def test_write_agents_keep_the_never_push_rule():
    for key in ("fix", "implement"):
        body = _AGENT_BASES[key]["body"].lower()
        assert "push" in body, key
        assert "never push" in body, key
