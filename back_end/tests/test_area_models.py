"""Pure-Python tests for the Area map primitives.

DB-bound flows (CRUD, accept/reject, stamping) live in test_area_service.py
against faked nodes; here we pin the pure logic: kind/status vocabularies,
key normalization, the derived stale rule, and the "/"-boundary key
hierarchy helpers.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from domains.areas.models import (
    AREA_EDIT_STATUSES,
    AREA_KINDS,
    area_is_stale,
    child_key_prefix_of,
    is_leaf,
)
from domains.areas.services.area_service import normalize_key


def test_area_kinds_vocabulary():
    assert AREA_KINDS == {"subsystem", "feature", "ignore"}


def test_area_edit_statuses_vocabulary():
    assert AREA_EDIT_STATUSES == {"pending", "accepted", "rejected"}


def _area(code_changed_at=None, last_reviewed_at=None):
    return SimpleNamespace(
        code_changed_at=code_changed_at, last_reviewed_at=last_reviewed_at
    )


def test_stale_is_derived_from_change_vs_review():
    now = datetime.now(UTC)
    assert not area_is_stale(_area())  # never touched
    assert area_is_stale(_area(code_changed_at=now))  # changed, never reviewed
    assert area_is_stale(
        _area(code_changed_at=now, last_reviewed_at=now - timedelta(hours=1))
    )
    assert not area_is_stale(
        _area(code_changed_at=now - timedelta(hours=1), last_reviewed_at=now)
    )


def test_child_key_prefix_of_requires_slash_boundary():
    assert child_key_prefix_of("backend", "backend/delivery")
    assert child_key_prefix_of("backend/delivery", "backend/delivery/convergence")
    # THE bug the helper exists to avoid: bare startswith would match this.
    assert not child_key_prefix_of("backend", "backend-jobs")
    assert not child_key_prefix_of("backend", "backend")  # not its own child
    assert not child_key_prefix_of("backend/delivery", "backend")


def test_is_leaf_over_enabled_keys():
    keys = ["backend", "backend/delivery", "backend/delivery/convergence", "frontend"]
    assert not is_leaf("backend", keys)
    assert not is_leaf("backend/delivery", keys)
    assert is_leaf("backend/delivery/convergence", keys)
    assert is_leaf("frontend", keys)
    # Sibling with a shared prefix does not demote a leaf.
    assert is_leaf("backend-jobs", keys)
    assert is_leaf("anything", [])


def test_normalize_key_kebab_cases_and_caps_length():
    assert normalize_key("Feature: Login with Email!") == "feature-login-with-email"
    assert normalize_key("  Convergence  ") == "convergence"
    assert normalize_key("") == ""
    assert len(normalize_key("x" * 300)) <= 120


def test_normalize_key_preserves_hierarchy():
    assert normalize_key("Backend/Delivery Queue") == "backend/delivery-queue"
    assert normalize_key("backend//delivery/") == "backend/delivery"
    assert normalize_key("/deployment/Terraform!") == "deployment/terraform"
