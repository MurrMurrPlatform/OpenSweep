"""update_rule must refuse to land on another rule's (event_type, channel_id,
repository_uid) identity — the same dedupe check create_rule already applies.

DB-free: SlackNotificationRule.nodes.filter is monkeypatched with a small
in-memory store, and the audit hook + save are stubbed."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.slack import service as slack_service
from domains.slack.schemas import UpdateSlackRuleRequest


pytestmark = pytest.mark.asyncio


def _rule(uid, event_type, channel_id, repository_uid="", org_uid="org-a"):
    return SimpleNamespace(
        uid=uid,
        org_uid=org_uid,
        event_type=event_type,
        channel_id=channel_id,
        channel_name="",
        repository_uid=repository_uid,
        enabled=True,
        created_by="u1",
    )


@pytest.fixture
def store(monkeypatch):
    rules: dict[str, SimpleNamespace] = {}

    class _Nodes:
        @staticmethod
        async def get_or_none(**kw):
            uid = kw.get("uid")
            return rules.get(uid)

        @staticmethod
        async def filter(**kw):
            return [
                r for r in rules.values()
                if all(getattr(r, k) == v for k, v in kw.items())
            ]

    async def fake_save(self):
        rules[self.uid] = self
        return self

    async def fake_audit(**_kw):
        return None

    async def fake_validate(_org_uid, _repository_uid):
        return None

    monkeypatch.setattr(
        slack_service, "SlackNotificationRule",
        SimpleNamespace(nodes=_Nodes),
    )
    # The service's rule.save() call — SimpleNamespace doesn't have one, so we
    # patch it in per-rule at insert time.
    monkeypatch.setattr(slack_service, "write_audit", fake_audit)
    monkeypatch.setattr(slack_service, "_validate_rule_repo", fake_validate)

    def add(rule):
        rule.save = fake_save.__get__(rule)  # type: ignore[assignment]
        rules[rule.uid] = rule
        return rule

    return SimpleNamespace(add=add, rules=rules)


async def test_update_that_would_collide_is_rejected(store):
    store.add(_rule("r1", "campaign.completed", "C1"))
    store.add(_rule("r2", "campaign.failed", "C1"))

    req = UpdateSlackRuleRequest(event_type="campaign.completed")  # would collide with r1
    with pytest.raises(HTTPException) as exc:
        await slack_service.update_rule("org-a", "r2", req, actor_uid="u1")
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


async def test_update_that_stays_unique_lands(store):
    store.add(_rule("r1", "campaign.completed", "C1"))
    store.add(_rule("r2", "campaign.failed", "C1"))

    req = UpdateSlackRuleRequest(channel_id="C2")
    rule = await slack_service.update_rule("org-a", "r2", req, actor_uid="u1")
    assert rule.channel_id == "C2"


async def test_updating_the_rule_to_its_own_identity_is_fine(store):
    # The dedupe check must not treat the rule itself as its own duplicate.
    store.add(_rule("r1", "campaign.completed", "C1"))
    req = UpdateSlackRuleRequest(channel_name="renamed")
    rule = await slack_service.update_rule("org-a", "r1", req, actor_uid="u1")
    assert rule.channel_name == "renamed"
