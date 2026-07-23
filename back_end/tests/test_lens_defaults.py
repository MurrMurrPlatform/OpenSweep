"""Per-kind default lens key map — pure unit tests (no DB, no async)."""

from domains.lenses.services import lens_service


def test_default_lens_keys_by_kind():
    assert "implementation-gaps" in lens_service.default_lens_keys("feature")
    assert "bugs" in lens_service.default_lens_keys("subsystem")
    # global defaults are exactly the lenses that have a global_agent_key
    assert set(lens_service.default_lens_keys("global")) == {
        "architecture-review", "implementation-gaps",
    }
    assert lens_service.default_lens_keys("nonsense") == []
