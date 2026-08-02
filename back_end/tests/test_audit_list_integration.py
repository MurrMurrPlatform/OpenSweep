"""GET /api/v1/audit against the real test Neo4j (localhost:7999).

The audit list is the one route that hand-writes its Cypher: visibility,
ordering and the page window all go to the database, because the log is the
fastest-growing label in the graph and reading it whole to hand back 100
rows got slower with every run the instance ever did.

Hand-written Cypher is only really checked by a database — a WHERE that
silently matches nothing, an ORDER BY that puts nulls first, or a SKIP/LIMIT
off by one all parse fine and pass any in-memory fake. Hence this file.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neomodel import adb

import api.v1.audit as audit_mod
from domains.events.models import Event
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.integration


def _repo() -> str:
    return "repo-" + uuid4().hex[:8]


def _user(*, org, platform=False):
    return UserDTO(
        uid="u", email="e@x.y", display_name="U", role="admin",
        org_uid=org, org_role="owner", is_platform_admin=platform,
    )


async def _event(repo: str, *, kind="k", actor="a", occurred_at=None) -> Event:
    e = Event(
        uid=uuid4().hex,
        kind=kind,
        subject_uid="s",
        subject_type="Run",
        actor_uid=actor,
        repository_uid=repo,
        payload={},
        occurred_at=occurred_at,
    )
    await e.save()
    return e


async def _undated_event(repo: str) -> Event:
    """An Event with no occurred_at — only reachable through raw Cypher.

    `Event.occurred_at` is `default_now=True`, so the ORM back-fills a date
    on save. Rows written before the property existed have none, and those
    are exactly the rows a plain `ORDER BY ... DESC` sorts to the top.
    """
    e = await _event(repo)
    await adb.cypher_query(
        "MATCH (e:Event {uid: $uid}) REMOVE e.occurred_at", {"uid": e.uid}
    )
    return e


async def _list(monkeypatch, *, allowed, user, **kwargs):
    async def org_repos(org_uid):
        return set(allowed)

    monkeypatch.setattr(audit_mod, "org_repo_uids", org_repos)
    params = dict(
        subject_type=None, subject_uid=None, kind=None, actor_uid=None,
        repository_uid=None, limit=100, offset=0,
    )
    params.update(kwargs)
    return await audit_mod.list_events(user=user, **params)


async def test_the_query_returns_only_the_callers_repos(monkeypatch):
    mine, theirs = _repo(), _repo()
    mine_event = await _event(mine)
    await _event(theirs)
    got = await _list(monkeypatch, allowed={mine}, user=_user(org="org-a"))
    assert {e.uid for e in got} == {mine_event.uid}


async def test_platform_events_reach_admins_only(monkeypatch):
    mine = _repo()
    await _event(mine)
    platform = await _event("")  # no repository — instance-level
    tenant = await _list(monkeypatch, allowed={mine}, user=_user(org="org-a"))
    assert platform.uid not in {e.uid for e in tenant}
    admin = await _list(
        monkeypatch, allowed={mine}, user=_user(org="org-a", platform=True)
    )
    assert platform.uid in {e.uid for e in admin}


async def test_newest_first_and_undated_events_sort_last(monkeypatch):
    repo = _repo()
    now = datetime.now(UTC)
    old = await _event(repo, occurred_at=now - timedelta(days=2))
    new = await _event(repo, occurred_at=now)
    # Cypher sorts null ABOVE every value on DESC; an undated legacy row must
    # not pin itself to the top of the log forever.
    undated = await _undated_event(repo)
    got = await _list(monkeypatch, allowed={repo}, user=_user(org="org-a"))
    assert [e.uid for e in got] == [new.uid, old.uid, undated.uid]


async def test_offset_and_limit_walk_the_log_without_gaps(monkeypatch):
    repo = _repo()
    now = datetime.now(UTC)
    for i in range(5):
        await _event(repo, occurred_at=now - timedelta(minutes=i))
    everything = await _list(monkeypatch, allowed={repo}, user=_user(org="org-a"))
    walked = []
    for offset in (0, 2, 4):
        page = await _list(
            monkeypatch, allowed={repo}, user=_user(org="org-a"),
            limit=2, offset=offset,
        )
        walked.extend(e.uid for e in page)
    assert walked == [e.uid for e in everything]


async def test_filters_and_tenancy_combine(monkeypatch):
    mine, theirs = _repo(), _repo()
    wanted = await _event(mine, kind="run.started", actor="alice")
    await _event(mine, kind="run.started", actor="bob")
    await _event(mine, kind="run.ended", actor="alice")
    await _event(theirs, kind="run.started", actor="alice")  # other tenant
    got = await _list(
        monkeypatch, allowed={mine}, user=_user(org="org-a"),
        kind="run.started", actor_uid="alice",
    )
    assert [e.uid for e in got] == [wanted.uid]


async def test_an_empty_repository_uid_still_returns_the_org(monkeypatch):
    # `?repository_uid=` binds "" and has always meant "no repo filter";
    # treating it as a named repo narrows to a uid that cannot exist.
    repo = _repo()
    mine = await _event(repo)
    got = await _list(
        monkeypatch, allowed={repo}, user=_user(org="org-a"), repository_uid=""
    )
    assert {e.uid for e in got} == {mine.uid}


async def test_a_foreign_repository_uid_returns_nothing(monkeypatch):
    mine, theirs = _repo(), _repo()
    await _event(theirs)
    got = await _list(
        monkeypatch, allowed={mine}, user=_user(org="org-a"), repository_uid=theirs
    )
    assert got == []
