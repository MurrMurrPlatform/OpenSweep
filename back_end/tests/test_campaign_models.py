"""Campaign status state machine + kind/selection/coverage model fields.

Pure tests, mirroring tests/test_thread_transitions.py: the full legality
matrix spelled out pairwise so a regression in either the dict or the
checker is caught.
"""

from unittest.mock import MagicMock

from domains.campaigns.models import (
    CAMPAIGN_KINDS,
    CAMPAIGN_SELECTIONS,
    CAMPAIGN_STATUSES,
    CAMPAIGN_TEMPLATES,
    LEGAL_STATUS_TRANSITIONS,
    PART_STATES,
    Campaign,
    is_legal_status_transition,
)
from domains.campaigns.schemas import CreateCampaignRequest
from domains.campaigns.services.campaign_service import to_dto

LEGAL = {
    ("planning", "running"),
    ("planning", "cancelled"),
    ("running", "finalizing"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("finalizing", "done"),
    ("finalizing", "failed"),
}


def test_full_status_transition_matrix():
    for frm in CAMPAIGN_STATUSES:
        for to in CAMPAIGN_STATUSES:
            expected = (frm, to) in LEGAL
            assert is_legal_status_transition(frm, to) == expected, f"{frm} → {to}"


def test_terminal_statuses_have_no_exits():
    assert not LEGAL_STATUS_TRANSITIONS["done"]
    assert not LEGAL_STATUS_TRANSITIONS["failed"]
    assert not LEGAL_STATUS_TRANSITIONS["cancelled"]


def test_self_transitions_are_illegal():
    for status in CAMPAIGN_STATUSES:
        assert not is_legal_status_transition(status, status)


def test_vocabulary():
    assert CAMPAIGN_STATUSES == {
        "planning",
        "running",
        "finalizing",
        "done",
        "failed",
        "cancelled",
    }
    assert CAMPAIGN_TEMPLATES == {"full", "rotation", "focused"}
    assert PART_STATES == {"pending", "running", "done", "failed"}


# ---------------------------------------------------------------------------
# New-field constants
# ---------------------------------------------------------------------------


def test_campaign_kinds_constant():
    assert CAMPAIGN_KINDS == {"subsystem", "feature", "global", "batch"}


def test_campaign_selections_constant():
    assert CAMPAIGN_SELECTIONS == {"all", "stale", "rotation"}


# ---------------------------------------------------------------------------
# to_dto round-trip for new fields
# ---------------------------------------------------------------------------


def test_to_dto_new_fields_round_trip():
    """Campaign(kind, selection, coverage_keys) must survive to_dto."""
    c = MagicMock(spec=Campaign)
    # Required fields
    c.uid = "uid-1"
    c.repository_uid = "repo-1"
    c.title = "My campaign"
    c.status = "planning"
    c.template = "rotation"
    c.effort = "normal"
    c.lens_keys = []
    c.k = 3
    c.area_prefix = ""
    c.parts = []
    c.max_parallel = 2
    c.created_by = ""
    c.trigger_provenance = ""
    c.summary = {}
    c.plan_summary = {}
    c.events = []
    c.created_at = None
    c.updated_at = None
    # New fields
    c.kind = "feature"
    c.selection = "stale"
    c.coverage_keys = ["x"]
    c.parent_uid = ""
    c.child_uids = []

    dto = to_dto(c)

    assert dto.kind == "feature"
    assert dto.selection == "stale"
    assert dto.coverage_keys == ["x"]
    assert dto.parent_uid == ""
    assert dto.child_uids == []


def test_to_dto_new_fields_defaults_when_absent():
    """to_dto must not crash on old Campaign nodes that lack the new attrs."""
    c = MagicMock(spec=Campaign)
    c.uid = "uid-2"
    c.repository_uid = "repo-2"
    c.title = ""
    c.status = "planning"
    c.template = "rotation"
    c.effort = ""
    c.lens_keys = []
    c.k = 3
    c.area_prefix = ""
    c.parts = []
    c.max_parallel = 2
    c.created_by = ""
    c.trigger_provenance = ""
    c.summary = {}
    c.plan_summary = {}
    c.events = []
    c.created_at = None
    c.updated_at = None
    # Simulate old node — new attrs raise AttributeError via spec
    del c.kind
    del c.selection
    del c.coverage_keys
    del c.parent_uid
    del c.child_uids

    dto = to_dto(c)

    assert dto.kind == "subsystem"
    assert dto.selection == "all"
    assert dto.coverage_keys == []
    assert dto.parent_uid == ""
    assert dto.child_uids == []


# ---------------------------------------------------------------------------
# CreateCampaignRequest new fields
# ---------------------------------------------------------------------------


def test_create_campaign_request_kind_global():
    req = CreateCampaignRequest(kind="global")
    assert req.kind == "global"
    assert req.coverage_keys == []
    assert req.selection == ""


def test_create_campaign_request_defaults():
    req = CreateCampaignRequest()
    assert req.kind == ""
    assert req.coverage_keys == []
    assert req.selection == ""
