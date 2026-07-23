"""Lens library tests — spec validation, checklist rendering, checksum
semantics.

Mirrors test_prompt_seeds.py (the seed specs are pinned pure-Python) and
test_seeding.py (the upsert decision table is driven against an in-memory
fake node): a SYNC must roll shipped lens bodies forward without ever
clobbering an org's tuning.
"""

from types import SimpleNamespace

import domains.lenses.services.seed_lenses as sl
from domains.agents.services.seed_variants import _VARIANTS
from domains.lenses.services.lens_service import lens_checklist
from domains.lenses.services.seed_lenses import (
    _LENSES,
    _checksum,
    _current_values,
    upsert_lens,
)
from infrastructure.seeding.base import SeedMode, content_hash

# asyncio_mode = "auto" (pyproject) runs the async tests; the sync ones stay sync.

_MAX_BODY_CHARS = 3000

_EXPECTED_LOCAL = {
    "bugs",
    "security",
    "simplification",
    "refactor-opportunities",
    "test-gaps",
    "error-handling",
    "performance",
    "legacy-patterns",
}
_EXPECTED_GLOBAL = {"architecture-review", "implementation-gaps"}


# ── pure: the seed specs ────────────────────────────────────────────────────


def test_seeded_lens_set_is_complete():
    local = {k for k, s in _LENSES.items() if not bool(s.get("global_agent_key"))}
    global_ = {k for k, s in _LENSES.items() if bool(s.get("global_agent_key"))}
    assert local == _EXPECTED_LOCAL
    assert global_ == _EXPECTED_GLOBAL


def test_lens_specs_are_well_formed():
    for key, spec in _LENSES.items():
        assert spec["title"], key
        assert spec["body"].strip(), key
        assert len(spec["body"]) <= _MAX_BODY_CHARS, f"{key}: body too long"
        assert isinstance(spec.get("tags", []), list), key
        assert isinstance(spec.get("wants", []), list), key
        # The lens_verdicts contract depends on agents reporting clean checks
        # instead of padding — every body must legitimize the empty result.
        assert "valid verdict" in spec["body"].lower(), key


def test_global_lenses_dispatch_a_seeded_variant():
    for key, spec in _LENSES.items():
        agent_key = spec.get("global_agent_key", "")
        if bool(agent_key):
            # Global lens: dispatch target must be a real seeded variant slug.
            assert agent_key in _VARIANTS, key
        else:
            # Local lens: must have no global_agent_key.
            assert not agent_key, key


def test_global_sweep_variants_check_their_escalation_queue():
    for key in _EXPECTED_GLOBAL:
        assert f"escalate:{key}" in _VARIANTS[key]["body"], key
        assert _VARIANTS[key]["produces"] == "findings", key
        assert _VARIANTS[key]["default_effort"] == "deep", key


# ── pure: checklist rendering ───────────────────────────────────────────────


def _lens(key: str, title: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, title=title, body=body)


def test_lens_checklist_numbers_titles_and_carries_the_escalate_rule():
    text = lens_checklist(
        [_lens("bugs", "Bugs", "Hunt bugs."), _lens("security", "Security", "Hunt vulns.")]
    )
    assert text.startswith("## Audit lenses for this scope")
    assert "verdict per lens in complete_run (lens_verdicts)" in text
    assert "### 1. Bugs" in text
    assert "### 2. Security" in text
    assert text.index("### 1. Bugs") < text.index("### 2. Security")
    assert "Hunt vulns." in text
    # Out-of-lane observations are escalated, never investigated locally.
    assert "escalate:<global-lens-key>" in text
    assert "escalate:architecture-review" in text
    assert "do NOT investigate" in text


def test_lens_checklist_falls_back_to_the_key_when_untitled():
    text = lens_checklist([_lens("bugs", "", "b")])
    assert "### 1. bugs" in text


# ── in-memory fake for the lens upsert (mirrors test_seeding._FakeAgent) ────


class _FakeLens:
    saved: list["_FakeLens"] = []

    def __init__(self, **kw):
        self.__dict__.update(
            uid=None, key="", title="", body="", tags=[],
            wants=[], global_agent_key="", enabled=True, provenance="",
            seed_checksum="",
        )
        self.__dict__.update(kw)

    async def save(self):
        if self not in _FakeLens.saved:
            _FakeLens.saved.append(self)
        return self


async def _no_existing(**kw):
    return []


def _patch_model(monkeypatch):
    _FakeLens.saved = []
    _FakeLens.nodes = SimpleNamespace(filter=lambda **kw: _no_existing(**kw))
    monkeypatch.setattr(sl, "Lens", _FakeLens)


def _spec(body="B1"):
    return {"title": "T", "tags": ["x"], "wants": [], "body": body}


def _existing(body="B1", seed_checksum=""):
    row = _FakeLens(provenance="system", key="bugs", body=body, title="T", tags=["x"])
    row.seed_checksum = seed_checksum
    return row


async def test_create_stamps_checksum(monkeypatch):
    _patch_model(monkeypatch)
    action = await upsert_lens(_spec(), "bugs", SeedMode.SYNC)
    assert action == "created"
    row = _FakeLens.saved[-1]
    assert row.provenance == "system" and row.key == "bugs" and row.body == "B1"
    assert row.seed_checksum == _checksum(sl._normalized(_spec()))


async def test_upsert_never_touches_existing(monkeypatch):
    _patch_model(monkeypatch)
    row = _existing(body="edited-by-org", seed_checksum="whatever")
    action = await upsert_lens(_spec(body="B2"), "bugs", SeedMode.UPSERT, existing=row)
    assert action == "unchanged"
    assert row.body == "edited-by-org"  # untouched


async def test_sync_rolls_forward_an_untouched_tracked_row(monkeypatch):
    _patch_model(monkeypatch)
    row = _existing(body="B1")
    row.seed_checksum = _checksum(_current_values(row))  # tracked, unedited
    action = await upsert_lens(_spec(body="B2"), "bugs", SeedMode.SYNC, existing=row)
    assert action == "updated"
    assert row.body == "B2"
    assert row.seed_checksum == _checksum(sl._normalized(_spec(body="B2")))


async def test_sync_preserves_an_org_edited_row(monkeypatch):
    _patch_model(monkeypatch)
    row = _existing(body="org-edited")
    # stored checksum is of some OLD shipped content, != current content hash
    row.seed_checksum = content_hash("old", "shipped", "content")
    action = await upsert_lens(_spec(body="B2"), "bugs", SeedMode.SYNC, existing=row)
    assert action == "preserved"
    assert row.body == "org-edited"


async def test_force_overwrites_even_an_org_edited_row(monkeypatch):
    _patch_model(monkeypatch)
    row = _existing(body="org-edited")
    row.seed_checksum = content_hash("anything")
    action = await upsert_lens(_spec(body="B2"), "bugs", SeedMode.FORCE, existing=row)
    assert action == "updated"
    assert row.body == "B2"


async def test_sync_adopts_a_legacy_row_only_when_it_matches_shipped(monkeypatch):
    _patch_model(monkeypatch)
    row = _existing(body="B1", seed_checksum="")
    action = await upsert_lens(_spec(body="B1"), "bugs", SeedMode.SYNC, existing=row)
    assert action == "unchanged"
    assert row.seed_checksum == _checksum(sl._normalized(_spec(body="B1")))

    divergent = _existing(body="something-else", seed_checksum="")
    action = await upsert_lens(_spec(body="B2"), "bugs", SeedMode.SYNC, existing=divergent)
    assert action == "preserved"
    assert divergent.seed_checksum == ""
