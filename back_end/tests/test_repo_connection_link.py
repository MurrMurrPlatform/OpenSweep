"""Linking a repository to a git credential — the operation that was missing.

WHY: `git_connection_uid` was written once, at registration, and never again.
But credentials outlive registration — tokens get rotated, App installations
get reinstalled, an org connects a second token for a different set of repos.
`delete_pat_connection` even documents that repos "keep their
git_connection_uid" when a connection is removed, which leaves them pointing at
a row that no longer exists with no supported way back. The only recovery was
DB access or re-registering the repo.

The link VERIFIES before it commits: recording a credential that cannot deliver
would just move the failure to the next run, which is the thing this exists to
prevent.
"""

import pytest
from fastapi import HTTPException

import domains.repositories.services.connection_link as cl

pytestmark = pytest.mark.asyncio

_STORE: dict[str, list] = {}
_AUDITS: list[dict] = []


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        store = _STORE.setdefault(type(self).__name__, [])
        if self not in store:
            store.append(self)
        return self


class FakeConn(_Node):
    class nodes:
        @staticmethod
        async def get_or_none(**kw):
            for n in _STORE.get("FakeConn", []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        @staticmethod
        async def filter(**kw):
            return [
                n
                for n in _STORE.get("FakeConn", [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]


class FakeRepo(_Node):
    pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    _AUDITS.clear()
    monkeypatch.setattr(cl, "GitConnection", FakeConn)

    async def audit(**kw):
        _AUDITS.append(kw)

    monkeypatch.setattr(cl, "write_audit", audit)
    yield
    _STORE.clear()
    _AUDITS.clear()


def _conn(uid, *, kind="pat", org="org1", account="someone", external_id=None):
    c = FakeConn(
        uid=uid,
        org_uid=org,
        kind=kind,
        provider="github",
        display_name=account,
        external_id=external_id if external_id is not None else f"pat:{uid}",
    )
    _STORE.setdefault("FakeConn", []).append(c)
    return c


def _repo(*, conn_uid="", installation=None):
    return FakeRepo(
        uid="r1",
        slug="demo",
        org_uid="org1",
        github_owner="acme",
        github_repo="demo",
        git_connection_uid=conn_uid,
        github_installation_id=installation,
        updated_at=None,
    )


def _access(monkeypatch, state):
    """Stub the shared write-access evaluation the link verifies against."""

    async def evaluate(repo):
        from domains.delivery.services.write_gate import WriteAccess

        return WriteAccess(
            state=state,
            credential="stub",
            reason="stub reason" if state in ("disconnected", "denied") else "",
        )

    monkeypatch.setattr(
        "domains.delivery.services.write_gate.evaluate_write_access", evaluate
    )


# ── Linking ──────────────────────────────────────────────────────────────


async def test_linking_a_pat_points_the_repo_at_it(monkeypatch):
    _conn("c-new")
    _access(monkeypatch, "ok")
    repo = _repo(conn_uid="c-dead")

    out = await cl.link_repository_connection(
        repo, connection_uid="c-new", org_uid="org1", actor_uid="u1"
    )

    assert out.git_connection_uid == "c-new"
    assert [a["kind"] for a in _AUDITS] == ["repository.connection_linked"]
    assert _AUDITS[0]["payload"]["previous"]["git_connection_uid"] == "c-dead"


async def test_linking_a_pat_clears_a_stale_installation_id(monkeypatch):
    """`get_repo_git_token` prefers an installation whenever one is set, so
    leaving the old id would silently ignore the token just chosen."""
    _conn("c-pat")
    _access(monkeypatch, "ok")
    repo = _repo(installation=4242)

    out = await cl.link_repository_connection(
        repo, connection_uid="c-pat", org_uid="org1", actor_uid="u1"
    )

    assert out.github_installation_id is None


async def test_linking_an_app_sets_the_installation_id(monkeypatch):
    _conn("c-app", kind="app", external_id="99")
    _access(monkeypatch, "ok")
    repo = _repo()

    out = await cl.link_repository_connection(
        repo, connection_uid="c-app", org_uid="org1", actor_uid="u1"
    )

    assert out.github_installation_id == 99
    assert out.git_connection_uid == "c-app"


async def test_a_connection_from_another_org_is_404(monkeypatch):
    """Without this, any uid could borrow another tenant's credential."""
    _conn("c-theirs", org="org2")
    _access(monkeypatch, "ok")

    with pytest.raises(HTTPException) as exc:
        await cl.link_repository_connection(
            _repo(), connection_uid="c-theirs", org_uid="org1", actor_uid="u1"
        )
    assert exc.value.status_code == 404


async def test_unusable_credential_is_rejected_and_rolled_back(monkeypatch):
    """A link that records a dead credential just defers the failure."""
    _conn("c-dud")
    _access(monkeypatch, "disconnected")
    repo = _repo(conn_uid="c-old", installation=7)

    with pytest.raises(HTTPException) as exc:
        await cl.link_repository_connection(
            repo, connection_uid="c-dud", org_uid="org1", actor_uid="u1"
        )

    assert exc.value.status_code == 422
    assert repo.git_connection_uid == "c-old", "must not leave the repo worse off"
    assert repo.github_installation_id == 7
    assert _AUDITS == [], "a rejected link is not a link"


async def test_read_only_credential_still_links(monkeypatch):
    """"denied" means the credential resolves but cannot push — linking it is
    legitimate (the fix is on GitHub's side), and the banner keeps saying so."""
    _conn("c-readonly")
    _access(monkeypatch, "denied")
    repo = _repo()

    out = await cl.link_repository_connection(
        repo, connection_uid="c-readonly", org_uid="org1", actor_uid="u1"
    )
    assert out.git_connection_uid == "c-readonly"
    assert _AUDITS[0]["payload"]["write_access"] == "denied"


# ── Candidates ───────────────────────────────────────────────────────────


async def test_candidates_are_org_scoped_and_flag_the_current_one():
    _conn("c1", account="alice")
    _conn("c2", account="bob")
    _conn("c-other", org="org2", account="mallory")

    out = await cl.connection_candidates(_repo(conn_uid="c2"), "org1")

    assert [c.uid for c in out] == ["c2", "c1"], "current first, then by account"
    assert [c.account for c in out if c.is_current] == ["bob"]
    assert "mallory" not in [c.account for c in out]


async def test_app_candidates_carry_their_installation_id():
    _conn("c-app", kind="app", external_id="123", account="acme-org")

    out = await cl.connection_candidates(_repo(installation=123), "org1")

    assert out[0].kind == "app"
    assert out[0].installation_id == 123
    assert out[0].is_current, "matched on installation id, not connection uid"


async def test_app_rows_with_unusable_external_ids_are_not_offered():
    """Offering a candidate that would 422 on selection is worse than hiding it."""
    _conn("c-bad", kind="app", external_id="not-a-number")

    assert await cl.connection_candidates(_repo(), "org1") == []
