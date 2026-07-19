"""Pure-Python tests for the Agent vocabulary (produces, triggers, keys)."""

import pytest

from domains.agents.models import PRODUCES
from domains.agents.schemas import parse_trigger
from domains.agents.services.registry import (
    AGENT_KEYS,
    PRODUCES_TO_PLAYBOOK,
    USER_CREATABLE_PRODUCES,
    agent_key,
    playbook_for_produces,
    source_url_for_key,
)
from domains.runs.services.playbooks import PLAYBOOKS


def test_every_produces_maps_to_a_real_playbook():
    assert set(PRODUCES_TO_PLAYBOOK) == PRODUCES
    for produces, playbook in PRODUCES_TO_PLAYBOOK.items():
        assert playbook in PLAYBOOKS, produces


def test_user_creatable_produces_is_a_subset_of_the_vocabulary():
    assert USER_CREATABLE_PRODUCES <= PRODUCES


def test_unknown_produces_falls_back_to_ask():
    assert playbook_for_produces("nonsense") == "ask"
    assert playbook_for_produces("") == "ask"


def test_parse_trigger_handles_all_three_modes():
    assert parse_trigger("") == ("manual", "")
    assert parse_trigger(None) == ("manual", "")
    assert parse_trigger("on-event") == ("on-event", "")
    assert parse_trigger("cron:0 9 * * 1") == ("cron", "0 9 * * 1")
    with pytest.raises(ValueError):
        parse_trigger("cron:")
    with pytest.raises(ValueError):
        parse_trigger("nonsense")


def test_agent_key_and_source_url_round_trip():
    for key in AGENT_KEYS:
        assert agent_key(source_url_for_key(key)) == key
    assert source_url_for_key("") == ""
    assert agent_key("") == ""
    # Non-opensweep URLs (imported/user rows) have no system key.
    assert agent_key("https://example.com/x") == ""
