"""system_agent_by_key must resolve library-variant bodies.

Regression pin for the resolution bug where a variant dispatched directly by
key (e.g. a scheduled `deep-issue-hunt`) silently degraded to the generic
"Run the <key> agent." fallback. `source_url_for_key` can only produce the
`opensweep://agent/<key>` form, so the variant row at
`opensweep://library/<slug>` was never found and the run executed with the
deep budget but an empty prompt body. The fix adds a library-URL fallback in
`system_agent_by_key`; these tests fail without it.
"""

from domains.agents.services import registry
from domains.agents.services.registry import (
    LIBRARY_URL_PREFIX,
    source_url_for_key,
    system_agent_by_key,
    variant_source_url,
)
from domains.agents.services.seed_variants import _VARIANTS


class _StubAgent:
    def __init__(self, *, source_url, prompt, enabled=True):
        self.source_url = source_url
        self.prompt = prompt
        self.enabled = enabled
        self.provenance = "system"


def _library_only_store(monkeypatch):
    """Simulate a DB where ONLY library rows exist — the exact shape that
    exposed the bug: source_url_for_key's agent/ URL misses every time."""
    rows = {
        variant_source_url(slug): _StubAgent(
            source_url=variant_source_url(slug),
            prompt=f"body for {slug}",
        )
        for slug in _VARIANTS
    }

    async def fake_by_url(source_url):
        return rows.get(source_url)

    monkeypatch.setattr(registry, "system_agent_by_url", fake_by_url)


async def test_every_variant_resolves_via_the_library_fallback(monkeypatch):
    _library_only_store(monkeypatch)
    for slug in _VARIANTS:
        row = await system_agent_by_key(slug)
        assert row is not None, slug
        assert row.prompt == f"body for {slug}"
        # Primary lookup targets the agent/ URL and MUST miss — proving the
        # non-empty body came from the library fallback, not a coincidence.
        assert not source_url_for_key(slug).startswith(LIBRARY_URL_PREFIX)


async def test_deep_issue_hunt_body_is_not_empty(monkeypatch):
    """The concrete symptom: the scheduled deep-issue-hunt got an empty body."""
    _library_only_store(monkeypatch)
    row = await system_agent_by_key("deep-issue-hunt")
    assert row is not None
    assert row.prompt.strip()


async def test_unknown_key_still_returns_none(monkeypatch):
    _library_only_store(monkeypatch)
    assert await system_agent_by_key("no-such-agent") is None
    assert await system_agent_by_key("") is None
