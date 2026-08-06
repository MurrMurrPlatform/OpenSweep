"""mark_all_read must upsert every unread visible item in a single Cypher call.

The old implementation issued a get_or_none + save per item, so at
FEED_WINDOW=300 unread events a single click could burn ~600 round-trips.
The batched form does one `UNWIND … MERGE` regardless of how many items
are being marked. DB-free — we spy on the seams the service uses.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.events.models import Event
from domains.notifications import service as feed
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _user():
    return UserDTO(
        uid="u1", email="e@x.y", display_name="U", role="admin",
        org_uid="org-a", org_role="owner", is_platform_admin=False,
    )


def _event(uid: str, minutes_ago: int = 0) -> Event:
    return Event(
        uid=uid,
        kind="run.ended",
        subject_uid=f"subj-{uid}",
        subject_type="Run",
        actor_uid="actor",
        repository_uid="repo-a",
        payload={},
        occurred_at=_NOW - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def spied(monkeypatch):
    events = [_event(f"e{i}", minutes_ago=i) for i in range(5)]

    async def fake_recent_events(*, repos, platform_events, kinds, limit=feed.FEED_WINDOW):
        return [e for e in events if e.repository_uid in repos][:limit]

    async def fake_read_states(user_uid, event_uids):
        return {}

    async def fake_org_repo_uids(org_uid):
        return {"repo-a"} if org_uid == "org-a" else set()

    calls: list[tuple[str, dict]] = []

    async def fake_cypher_query(query: str, params: dict):
        calls.append((query, params))
        return [], []

    monkeypatch.setattr(feed, "_recent_events", fake_recent_events)
    monkeypatch.setattr(feed, "_read_states", fake_read_states)
    monkeypatch.setattr(feed, "org_repo_uids", fake_org_repo_uids)
    monkeypatch.setattr(feed.adb, "cypher_query", fake_cypher_query)
    return SimpleNamespace(events=events, calls=calls)


async def test_mark_all_read_issues_one_write(spied):
    n = await feed.mark_all_read(_user())
    assert n == 5
    # One batched UNWIND/MERGE — not one save per item.
    assert len(spied.calls) == 1
    query, params = spied.calls[0]
    assert "UNWIND" in query and "MERGE" in query
    assert [r["event_uid"] for r in params["rows"]] == [f"e{i}" for i in range(5)]
    assert all(r["user_uid"] == "u1" for r in params["rows"])
    # ON MATCH SET preserves any earlier read_at via coalesce.
    assert "coalesce" in query


async def test_mark_all_read_noop_when_nothing_unread(spied, monkeypatch):
    async def empty_recent(*_, **__):
        return []

    monkeypatch.setattr(feed, "_recent_events", empty_recent)
    n = await feed.mark_all_read(_user())
    assert n == 0
    assert spied.calls == []
