"""Accepting a whole proposed map — the batch the map-areas run produces.

A mapping run proposes the subsystem axis and the feature axis together, as
one batch of pending AreaEdits. These tests pin the two invariants that batch
depends on: accepting an edit must judge it against the map the batch as a
whole describes (not against whichever siblings happen to be applied first),
and a retirement proposal must never bring an area into existence.
"""



from domains.areas.services import area_service

from .test_area_service import (  # noqa: F401 — `stores` is a fixture
    stores,
)


async def _propose(**kwargs):
    kwargs.setdefault("repository_uid", "r1")
    kwargs.setdefault("proposed_spec", "a reason")
    kwargs.setdefault("source_run_uid", "run-1")
    return await area_service.propose_area_edit(**kwargs)


async def test_accepting_a_feature_before_its_subsystems_does_not_warn(stores):
    """The feature-span check must see the batch, not the accept order.

    A map run proposes subsystems first, then the features that overlay them.
    The review queue lists newest-first, so bulk accept applies the FEATURES
    first — at which point no subsystem Area exists yet. The feature is
    correctly anchored (its scope spans two subsystem leaves in the same
    batch); accepting it must not manufacture a warning that says otherwise.
    """
    await _propose(key="backend", scope_paths=["back_end/domains/runs/"])
    await _propose(key="frontend", scope_paths=["front_end/src/stores/"])
    feature = await _propose(
        key="features/execution",
        kind="feature",
        scope_paths=["back_end/domains/runs/", "front_end/src/stores/"],
    )

    _area, warnings = await area_service.accept_area_edit(feature["area_edit_uid"])

    assert warnings == []


async def test_accepting_a_retirement_for_an_unmapped_key_creates_nothing(stores):
    """`enabled=False` proposes RETIRING an area — never creating one.

    A run that proposes an area and then thinks better of it re-proposes the
    same key with enabled=false. On a first run that key has no Area behind
    it, so the edit carries no area_uid; accepting it must resolve the edit
    without materialising the area the agent just disowned.
    """
    proposal = await _propose(
        key="features/frontend-only",
        kind="feature",
        scope_paths=["front_end/src/"],
        enabled=False,
    )

    area, _warnings = await area_service.accept_area_edit(proposal["area_edit_uid"])

    assert area is None or area.enabled is False
    assert [a for a in stores.areas if a.enabled] == []
