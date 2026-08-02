"""Seeding framework tests — SeedMode semantics, registry selection, hashing.

DB-free: the platform-prompt upsert is driven against an in-memory fake node,
mirroring the fake-node pattern in test_llm_provider_tenancy.py. What we pin
here is the decision table (create / unchanged / updated / preserved) for each
SeedMode, because that is the logic prod correctness depends on — a SYNC must
roll shipped defaults forward without ever clobbering a user's edit.
"""

from types import SimpleNamespace

import pytest

import domains.agents.services.platform_prompts as pp
from domains.agents.services.platform_prompts import (
    _checksum,
    _current_values,
    upsert_platform_prompt,
)
from infrastructure.seeding.base import SeedMode, SeedResult, content_hash
from infrastructure.seeding.registry import DEV, PLATFORM, SEEDERS, select_seeders

# asyncio_mode = "auto" (pyproject) runs the async tests; the sync ones stay sync.

URL = "opensweep://workflow/ask"


# ── pure: content_hash ──────────────────────────────────────────────────────


def test_content_hash_is_stable_and_order_sensitive():
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("b", "a")
    # NUL separator: ("a","bc") must not collide with ("ab","c").
    assert content_hash("a", "bc") != content_hash("ab", "c")
    assert content_hash(None) == content_hash(None)


def test_seed_result_as_dict_hides_empty_extras():
    d = SeedResult(name="x", created=2).as_dict()
    assert d == {"created": 2, "updated": 0, "unchanged": 0, "preserved": 0}
    assert "note" not in d and "error" not in d
    assert SeedResult(name="x", note="n", error="e").as_dict()["note"] == "n"


# ── pure: registry selection ────────────────────────────────────────────────


def test_platform_group_excludes_dev_seeders():
    names = {s.name for s in select_seeders({PLATFORM})}
    assert "local_user" not in names and "llm_providers" not in names
    assert "workflow_default_prompts" in names


def test_names_filter_restricts_within_group_and_keeps_order():
    picked = select_seeders({PLATFORM}, {"variant_prompts", "system_run_policy"})
    assert [s.name for s in picked] == ["system_run_policy", "variant_prompts"]


def test_all_groups_include_dev():
    names = {s.name for s in select_seeders({PLATFORM, DEV})}
    assert {"local_user", "llm_providers"} <= names


def test_every_seeder_has_a_known_group():
    assert all(s.group in {PLATFORM, DEV} for s in SEEDERS)


# ── in-memory fake for the prompt upsert ────────────────────────────────────


class _FakeAgent:
    """Minimal stand-in for Agent with the fields the upsert touches."""

    saved: list["_FakeAgent"] = []

    def __init__(self, **kw):
        self.__dict__.update(
            uid=None, org_uid="", title="", description="", prompt="",
            produces="findings", default_effort="normal", reasoning="",
            tags=[], provenance="", source_url="", source_commit="",
            seed_checksum="", rev=0, enabled=True,
        )
        self.__dict__.update(kw)

    async def save(self):
        if self not in _FakeAgent.saved:
            _FakeAgent.saved.append(self)
        return self


async def _no_existing(**kw):
    return []


@pytest.fixture(autouse=True)
def _fake_prompt_model(monkeypatch):
    _FakeAgent.saved = []
    # Lookup path returns nothing; tests that exercise existing rows pass
    # `existing=` directly instead.
    _FakeAgent.nodes = SimpleNamespace(filter=lambda **kw: _no_existing(**kw))
    monkeypatch.setattr(pp, "Agent", _FakeAgent)
    return _FakeAgent


def _spec(body="B1", tags=("x",)):
    return {
        "title": "T",
        "description": "D",
        "body": body,
        "tags": list(tags),
    }


def _existing(body="B1", seed_checksum=""):
    row = _FakeAgent(provenance="system", source_url=URL, prompt=body, title="T",
                     description="D", tags=["x"])
    row.seed_checksum = seed_checksum
    return row


async def test_create_stamps_checksum():
    action = await upsert_platform_prompt(_spec(), URL, SeedMode.SYNC)
    assert action == "created"
    row = _FakeAgent.saved[-1]
    assert row.provenance == "system" and row.prompt == "B1"
    assert row.seed_checksum == _checksum(pp._normalized(_spec()))


async def test_upsert_never_touches_existing():
    row = _existing(body="edited-by-user", seed_checksum="whatever")
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.UPSERT, existing=row)
    assert action == "unchanged"
    assert row.prompt == "edited-by-user"  # untouched


async def test_sync_rolls_forward_an_untouched_tracked_row():
    row = _existing(body="B1")
    row.seed_checksum = _checksum(_current_values(row))  # tracked, unedited
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.SYNC, existing=row)
    assert action == "updated"
    assert row.prompt == "B2"
    assert row.seed_checksum == _checksum(pp._normalized(_spec(body="B2")))


async def test_sync_preserves_a_user_edited_row():
    row = _existing(body="user-edited")
    # stored checksum is of some OLD shipped content, != current content hash
    row.seed_checksum = content_hash("old", "shipped", "content")
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.SYNC, existing=row)
    assert action == "preserved"
    assert row.prompt == "user-edited"


async def test_force_overwrites_even_a_user_edited_row():
    row = _existing(body="user-edited")
    row.seed_checksum = content_hash("anything")
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.FORCE, existing=row)
    assert action == "updated"
    assert row.prompt == "B2"


async def test_force_overwrites_a_user_edit_whose_stored_checksum_is_stale():
    # Regression: user edited the body (API edit leaves seed_checksum stale), and
    # the stored checksum happens to equal the shipped hash. FORCE must compare
    # CURRENT content, not the stale stored checksum, and still overwrite.
    shipped = _checksum(pp._normalized(_spec(body="B2")))
    row = _existing(body="user-edited")
    row.seed_checksum = shipped  # stale: predates the user's edit
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.FORCE, existing=row)
    assert action == "updated"
    assert row.prompt == "B2"


async def test_sync_adopts_a_legacy_row_only_when_it_matches_shipped():
    # Legacy (seed_checksum=="") whose content equals shipped → adopt + track.
    row = _existing(body="B1", seed_checksum="")
    action = await upsert_platform_prompt(_spec(body="B1"), URL, SeedMode.SYNC, existing=row)
    assert action == "unchanged"
    assert row.seed_checksum == _checksum(pp._normalized(_spec(body="B1")))


async def test_sync_preserves_a_legacy_row_that_differs_from_shipped():
    # Legacy row differing from shipped: can't prove it's unedited → preserve,
    # leave the empty checksum so it stays user-owned.
    row = _existing(body="something-else", seed_checksum="")
    action = await upsert_platform_prompt(_spec(body="B2"), URL, SeedMode.SYNC, existing=row)
    assert action == "preserved"
    assert row.prompt == "something-else"
    assert row.seed_checksum == ""


# ── in-memory fake for the provider seeder ──────────────────────────────────


class _FakeProvider:
    store: list["_FakeProvider"] = []

    def __init__(self, **kw):
        self.__dict__.update(
            uid=None, org_uid="", label="", kind="", base_url="", model="",
            api_key_env="", cli_command_template="", credential_secret="",
            enabled=True, active=False,
        )
        self.__dict__.update(kw)

    async def save(self):
        if self not in _FakeProvider.store:
            _FakeProvider.store.append(self)
        return self


@pytest.fixture
def _fake_provider_model(monkeypatch):
    import infrastructure.seeding.dev_seeders as ds

    _FakeProvider.store = []

    async def _all():
        return list(_FakeProvider.store)

    _FakeProvider.nodes = SimpleNamespace(all=_all)
    monkeypatch.setattr(ds, "LLMProvider", _FakeProvider)
    return ds


async def test_providers_activate_baseline_on_a_fresh_install(_fake_provider_model):
    ds = _fake_provider_model
    res = await ds.seed_llm_providers(SeedMode.UPSERT)
    assert res.created == 2
    active = [p for p in _FakeProvider.store if p.active]
    assert len(active) == 1 and active[0].label == "Claude Code (subscription)"
    # Providers are strictly org-owned: every seeded row belongs to the local org.
    assert all(p.org_uid == "local-org" for p in _FakeProvider.store)


async def test_reseed_never_steals_the_users_active_choice(_fake_provider_model):
    ds = _fake_provider_model
    # User's world: claude row present but INACTIVE; a different provider active.
    _FakeProvider.store = [
        _FakeProvider(uid="c", label="Claude Code (subscription)",
                      kind="claude_subscription", org_uid="local-org", active=False),
        _FakeProvider(uid="x", label="My Local", kind="mlx",
                      org_uid="local-org", active=True),
    ]
    await ds.seed_llm_providers(SeedMode.UPSERT)
    by_label = {p.label: p for p in _FakeProvider.store}
    assert by_label["My Local"].active is True          # user's choice untouched
    assert by_label["Claude Code (subscription)"].active is False
    assert sum(1 for p in _FakeProvider.store if p.active) == 1


async def test_reseed_backfills_org_uid_on_legacy_unowned_rows(_fake_provider_model):
    ds = _fake_provider_model
    # Pre-tenancy row: matched by label but carrying no org_uid.
    _FakeProvider.store = [
        _FakeProvider(uid="c", label="Claude Code (subscription)",
                      kind="claude_subscription", org_uid="", active=True),
    ]
    res = await ds.seed_llm_providers(SeedMode.UPSERT)
    row = next(p for p in _FakeProvider.store if p.uid == "c")
    assert row.org_uid == "local-org"
    assert res.updated >= 1  # the backfill counts as a touch
