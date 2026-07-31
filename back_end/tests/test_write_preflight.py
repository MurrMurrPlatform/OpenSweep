"""Write-access preflight, credential attribution, and argv redaction.

WHY: an implement run cloned fine and then 403'd at `git push` — read access is
enough to clone, so the first time write permission was exercised was delivery,
after a whole run had been spent. Three things made that hard to act on:

  1. Nothing asked whether the credential could push until the push.
  2. `GET /repos`.`permissions.push` reports the authenticated USER's role, not
     the credential's grant, so a read-only fine-grained PAT held by a repo
     admin reports push:true — checking it would have said "fine".
  3. When a repo is registered to an App installation but no App is loadable,
     resolution silently substitutes a human's PAT and logged nothing.

The preflight must fail CLOSED only on an unambiguous denial: blocking on a
timeout or an unrecognized status would convert a GitHub blip into a refusal to
work, which is worse than the late failure it replaces.
"""

import pytest

import domains.delivery.services.write_gate as wg

pytestmark = pytest.mark.asyncio


class _Repo:
    def __init__(self, owner="acme", name="demo"):
        self.github_owner = owner
        self.github_repo = name
        self.provider = "github"
        self.github_installation_id = None
        self.git_connection_uid = ""


class _Client:
    """Stands in for the provider client; `verdict` is what the probe returns."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls: list[tuple[str, str]] = []

    async def check_write_access(self, owner, repo):
        self.calls.append((owner, repo))
        return self.verdict


def _wire(monkeypatch, client, credential="the PAT connected as 'someone'", token="tok"):
    monkeypatch.setattr(
        "infrastructure.git_providers.get_provider_client", lambda repo: client
    )

    async def credentials(repo):
        return token

    monkeypatch.setattr("infrastructure.git_providers.get_git_credentials", credentials)

    async def describe(repo):
        return credential

    monkeypatch.setattr("infrastructure.github_app.describe_repo_credential", describe)


# ── The preflight verdict ────────────────────────────────────────────────


async def test_denial_is_reported_with_the_credential_named(monkeypatch):
    client = _Client(False)
    _wire(monkeypatch, client, credential="the PAT connected as 'jgcbrouns'")

    reason = await wg.write_access_denial_reason(_Repo("MurrMurrPlatform", "OpenSweep"))

    assert reason, "an explicit denial must block the dispatch"
    assert "jgcbrouns" in reason, "name the credential, not just the repo"
    assert "MurrMurrPlatform/OpenSweep" in reason
    assert "Contents" in reason, "say which grant to turn on"
    assert client.calls == [("MurrMurrPlatform", "OpenSweep")]


async def test_granted_write_does_not_block(monkeypatch):
    _wire(monkeypatch, _Client(True))
    assert await wg.write_access_denial_reason(_Repo()) == ""


async def test_inconclusive_probe_does_not_block(monkeypatch):
    """None means "couldn't tell" — proceed, exactly as before the preflight."""
    _wire(monkeypatch, _Client(None))
    assert await wg.write_access_denial_reason(_Repo()) == ""


async def test_provider_without_the_capability_does_not_block(monkeypatch):
    class _Bare:
        pass

    _wire(monkeypatch, _Bare())
    assert await wg.write_access_denial_reason(_Repo()) == ""


async def test_missing_credential_blocks_before_the_probe(monkeypatch):
    """Rotating a PAT deletes the connection but leaves every repo's
    `git_connection_uid` pointing at it, so the repo silently has no credential
    at all. That used to surface as an empty-token push failure after the run."""
    client = _Client(True)
    _wire(
        monkeypatch,
        client,
        credential="the git connection this repository was registered through (cee20c1f) no longer exists",
        token="",
    )

    reason = await wg.write_access_denial_reason(_Repo("acme", "demo"))

    assert "No usable GitHub credential" in reason
    assert "no longer exists" in reason, "say WHY there is no credential"
    assert client.calls == [], "pointless to probe with nothing to probe with"


async def test_repo_without_github_coordinates_is_skipped(monkeypatch):
    client = _Client(False)
    _wire(monkeypatch, client)

    assert await wg.write_access_denial_reason(_Repo(owner="", name="")) == ""
    assert client.calls == [], "nothing to probe without owner/repo"


# ── The probe itself ─────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _client_with(monkeypatch, responder):
    from infrastructure.github_client import GitHubClient

    c = GitHubClient(token="t")

    async def post(path, json=None, headers=None):
        return responder(path, json)

    monkeypatch.setattr(c._client, "post", post)
    return c


async def test_probe_maps_403_to_denied(monkeypatch):
    c = _client_with(monkeypatch, lambda path, json: _Resp(403))
    assert await c.check_write_access("acme", "demo") is False


async def test_probe_maps_422_to_allowed(monkeypatch):
    """422 = authorization passed, payload refused — which is the point: the
    null SHA cannot exist, so validation is the only thing left to fail on."""
    seen = {}

    def responder(path, json):
        seen.update(path=path, json=json)
        return _Resp(422)

    c = _client_with(monkeypatch, responder)
    assert await c.check_write_access("acme", "demo") is True
    assert seen["path"] == "/repos/acme/demo/git/refs"
    assert seen["json"]["sha"] == "0" * 40, "must be an impossible object, never a real one"


async def test_probe_treats_unknown_status_as_inconclusive(monkeypatch):
    for status in (200, 404, 500, 502):
        c = _client_with(monkeypatch, lambda path, json, s=status: _Resp(s))
        assert await c.check_write_access("acme", "demo") is None, status


async def test_probe_swallows_transport_errors(monkeypatch):
    def boom(path, json):
        raise RuntimeError("connection reset")

    c = _client_with(monkeypatch, boom)
    assert await c.check_write_access("acme", "demo") is None


async def test_probe_is_inert_without_a_credential():
    from infrastructure.github_client import GitHubClient

    assert await GitHubClient(token="").check_write_access("acme", "demo") is None
