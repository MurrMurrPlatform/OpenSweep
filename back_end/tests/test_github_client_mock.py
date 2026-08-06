"""GitHubClient — httpx.MockTransport based tests, no network."""

import json
from types import SimpleNamespace

import httpx
import pytest

from infrastructure.github_client import GitHubClient, MissingCredentialError


@pytest.mark.asyncio
async def test_inactive_when_token_unset():
    c = GitHubClient(token="")
    assert c.is_active is False
    with pytest.raises(RuntimeError):
        await c._get("/anything")
    await c.aclose()


@pytest.mark.asyncio
async def test_empty_token_source_raises_missing_credential():
    """A token source resolving to "" must fail with a named error, not send
    `Authorization: Bearer ` — httpx rejects that header as
    LocalProtocolError, which names the transport instead of the real
    problem (a deleted GitConnection with no fallback PAT)."""

    class _Empty:
        async def get_token(self) -> str:
            return ""

    c = GitHubClient(token_source=_Empty())
    # A source exists, so is_active can't know it resolves empty — the
    # request path is where it must surface.
    assert c.is_active is True
    with pytest.raises(MissingCredentialError):
        await c._get("/repos/acme/repo")
    await c.aclose()


@pytest.mark.asyncio
async def test_list_open_issues_parses_response(monkeypatch):
    body = [
        {"number": 1, "title": "First", "body": "b", "state": "open",
         "user": {"login": "alice"}, "labels": [{"name": "bug"}],
         "created_at": "2025-01-01T00:00:00Z"},
        # PRs should be filtered out by the service layer (this client returns raw).
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" in request.headers
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com",
                                   headers={"Authorization": "Bearer x", "Accept": "application/vnd.github+json"})
    out = await c.list_open_issues("acme", "repo")
    assert out[0]["number"] == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_open_pull_request_posts_correct_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"number": 42, "html_url": "https://github.com/x/y/pull/42"})

    transport = httpx.MockTransport(handler)
    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com",
                                   headers={"Authorization": "Bearer x"})
    pr = await c.open_pull_request("acme", "repo", head="feat/x", base="main", title="t", body="b")
    assert pr["number"] == 42
    assert "/repos/acme/repo/pulls" in captured["url"]
    assert captured["json"] == {"head": "feat/x", "base": "main", "title": "t", "body": "b", "draft": False}
    await c.aclose()


# ── Check-run pagination (CI rollup must not truncate at 30) ────────────────


def _check_run_page(start: int, count: int) -> dict:
    return {
        "total_count": 130,
        "check_runs": [
            {"name": f"check-{i}", "status": "completed", "conclusion": "success"}
            for i in range(start, start + count)
        ],
    }


@pytest.mark.asyncio
async def test_list_check_runs_paginates_and_filters_latest():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json=_check_run_page(100, 30))
        return httpx.Response(
            200,
            json=_check_run_page(0, 100),
            headers={
                "Link": '<https://api.github.com/repos/acme/repo/commits/abc/check-runs'
                '?per_page=100&filter=latest&page=2>; rel="next"'
            },
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    runs = await c.list_check_runs("acme", "repo", ref="abc")
    assert len(runs) == 130  # both pages, not just the first 30/100
    assert runs[0]["name"] == "check-0"
    assert runs[-1]["name"] == "check-129"
    # First request asks for big pages and only the latest check attempt.
    assert "per_page=100" in seen_urls[0]
    assert "filter=latest" in seen_urls[0]
    assert len(seen_urls) == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_list_check_runs_caps_defensively():
    from infrastructure.github_client import MAX_CHECK_RUNS

    def handler(request: httpx.Request) -> httpx.Response:
        # Every page claims another next page — a pathological rollup.
        return httpx.Response(
            200,
            json=_check_run_page(0, 100),
            headers={
                "Link": '<https://api.github.com/repos/acme/repo/commits/abc/check-runs'
                '?per_page=100&page=99>; rel="next"'
            },
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    runs = await c.list_check_runs("acme", "repo", ref="abc")
    assert len(runs) == MAX_CHECK_RUNS  # capped, no infinite pagination
    await c.aclose()


# ── Recursive tree listing (repo-wide scope for area planning) ──────────────


@pytest.mark.asyncio
async def test_get_tree_returns_blob_paths_and_truncated_flag():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "sha": "abc",
                "tree": [
                    {"path": "src/app.py", "type": "blob"},
                    {"path": "src", "type": "tree"},  # directories are not scope
                    {"path": "vendored", "type": "commit"},  # nor submodules
                    {"path": "README.md", "type": "blob"},
                ],
                "truncated": False,
            },
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    out = await c.get_tree("acme", "repo", "abc")
    assert out == {"paths": ["src/app.py", "README.md"], "truncated": False}
    assert "/repos/acme/repo/git/trees/abc" in seen_urls[0]
    assert "recursive=1" in seen_urls[0]
    await c.aclose()


@pytest.mark.asyncio
async def test_get_tree_surfaces_truncation_and_honors_recursive_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "recursive" not in str(request.url)
        return httpx.Response(
            200, json={"tree": [{"path": "a.py", "type": "blob"}], "truncated": True}
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    out = await c.get_tree("acme", "repo", "HEAD", recursive=False)
    assert out["truncated"] is True
    assert out["paths"] == ["a.py"]
    await c.aclose()


# ── Loop-aware default client (Celery: fresh loop per asyncio.run) ──────────


def test_get_default_client_is_not_reused_across_event_loops():
    import asyncio

    from infrastructure.github_client import get_default_client

    async def grab():
        return get_default_client()

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    # A client cached on a dead loop must never resurface on a new loop.
    assert first is not second


def test_get_default_client_is_cached_within_one_loop():
    import asyncio

    from infrastructure.github_client import get_default_client

    async def grab_twice():
        return get_default_client(), get_default_client()

    a, b = asyncio.run(grab_twice())
    assert a is b


# ── Retry/backoff on transient GitHub errors (429/5xx/secondary-rate-limit) ──


@pytest.mark.asyncio
async def test_get_retries_on_429_then_succeeds(monkeypatch):
    from infrastructure import github_client as gh

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"number": 1})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    out = await c.get_pull_request("acme", "repo", 1)
    assert out == {"number": 1}
    assert calls["n"] == 2  # one 429, one success — no more
    assert sleeps == [0.0]
    await c.aclose()


async def _noop():
    return None


@pytest.mark.asyncio
async def test_5xx_retries_exhaust_and_still_raise(monkeypatch):
    from infrastructure.github_client import MAX_GITHUB_RETRIES
    from infrastructure import github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"message": "service unavailable"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_pull_request("acme", "repo", 1)
    # Initial attempt + MAX_GITHUB_RETRIES retries, then give up.
    assert calls["n"] == MAX_GITHUB_RETRIES + 1
    await c.aclose()


@pytest.mark.asyncio
async def test_ordinary_403_is_not_retried(monkeypatch):
    from infrastructure import github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"message": "must have admin rights"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_pull_request("acme", "repo", 1)
    assert calls["n"] == 1  # a real permission denial must fail immediately
    await c.aclose()


@pytest.mark.asyncio
async def test_secondary_rate_limited_403_is_retried(monkeypatch):
    from infrastructure import github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, json={"message": "You have triggered an abuse detection mechanism"})
        return httpx.Response(200, json={"number": 1})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    out = await c.get_pull_request("acme", "repo", 1)
    assert out == {"number": 1}
    assert calls["n"] == 2
    await c.aclose()


# ── Branch protection: opensweep/converged as a required status check ──────


@pytest.mark.asyncio
async def test_add_required_status_check_creates_rule_when_none_exists():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Branch not protected"})
        assert request.method == "PUT"
        return httpx.Response(200, json={"required_status_checks": {"contexts": ["opensweep/converged"]}})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    outcome = await c.add_required_status_check("acme", "repo", "main", context="opensweep/converged")
    assert outcome == "created"
    assert [r.method for r in requests] == ["GET", "PUT"]
    import json as _json

    body = _json.loads(requests[1].content.decode())
    assert body["required_status_checks"]["contexts"] == ["opensweep/converged"]
    assert body["restrictions"] is None
    await c.aclose()


@pytest.mark.asyncio
async def test_add_required_status_check_leaves_existing_rule_untouched():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"required_status_checks": {"strict": True, "contexts": ["ci/build"]}},
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    outcome = await c.add_required_status_check("acme", "repo", "main", context="opensweep/converged")
    assert outcome == "not-required"  # context isn't in the existing rule — left alone
    assert [r.method for r in requests] == ["GET"]  # never attempted a PUT
    await c.aclose()


@pytest.mark.asyncio
async def test_add_required_status_check_reports_already_satisfied():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"required_status_checks": {"strict": True, "contexts": ["opensweep/converged"]}},
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    outcome = await c.add_required_status_check("acme", "repo", "main", context="opensweep/converged")
    assert outcome == "already-required"
    await c.aclose()


@pytest.mark.asyncio
async def test_add_required_status_check_no_admin_rights_degrades_quietly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Must have admin rights"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    outcome = await c.add_required_status_check("acme", "repo", "main", context="opensweep/converged")
    assert outcome == "failed"
    await c.aclose()


@pytest.mark.asyncio
async def test_add_required_status_check_403_read_never_blind_puts():
    """A 403 on the protection READ means "can't see it", not "there is none".
    Treating it as unprotected would PUT a fresh rule over the repo's real
    one, silently dropping required reviewers/restrictions."""
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(403, json={"message": "Must have admin rights"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    assert await c.add_required_status_check("acme", "repo", "main", context="opensweep/converged") == "failed"
    assert methods == ["GET"]  # no PUT was ever attempted
    with pytest.raises(PermissionError):
        await c.get_branch_protection("acme", "repo", "main")
    await c.aclose()


# ── Retry safety: unsafe methods and the backoff ceiling ────────────────────


@pytest.mark.asyncio
async def test_post_is_not_retried_on_5xx(monkeypatch):
    """A 5xx on a POST is ambiguous — GitHub may have applied the write before
    failing to answer. Replaying it duplicates PR reviews and issue comments,
    so creates must fail fast instead."""
    from infrastructure import github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, json={"message": "bad gateway"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.create_review("acme", "repo", 1, body="hi")
    assert calls["n"] == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_post_is_retried_on_429_because_it_definitely_did_not_apply(monkeypatch):
    from infrastructure import github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate limited"})
        return httpx.Response(201, json={"id": 7})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    assert await c.create_review("acme", "repo", 1, body="hi") == {"id": 7}
    assert calls["n"] == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_rate_limit_reset_beyond_ceiling_fails_fast_instead_of_sleeping(monkeypatch):
    """X-RateLimit-Reset an hour out must NOT park a worker/request for an
    hour (×4 attempts). Past the ceiling we surface the 429 immediately."""
    import time as _time

    from infrastructure import github_client as gh

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())
    calls = {"n": 0}
    reset = _time.time() + 3600

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"X-RateLimit-Reset": str(reset)}, json={"message": "rate limited"})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_pull_request("acme", "repo", 1)
    assert calls["n"] == 1  # returned the 429 rather than waiting an hour
    assert sleeps == []
    await c.aclose()


@pytest.mark.asyncio
async def test_backoff_is_capped_at_the_ceiling(monkeypatch):
    from infrastructure import github_client as gh
    from infrastructure.github_client import MAX_RETRY_DELAY_SECONDS

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})  # no hint headers

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_pull_request("acme", "repo", 1)
    assert sleeps and max(sleeps) <= MAX_RETRY_DELAY_SECONDS
    await c.aclose()


@pytest.mark.asyncio
async def test_installation_token_mint_retries_transient_5xx(monkeypatch):
    """Minting is the FIRST call in every delivery op on an App-connected
    repo — a transient failure there bypasses every other retry."""
    from infrastructure import github_app, github_client as gh

    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: _noop())
    monkeypatch.setattr(github_app, "make_app_jwt", lambda app_id, pem: "jwt")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        github_app.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    body = await github_app._request_installation_token(
        SimpleNamespace(app_id="1", pem="pem"), 42
    )
    assert body["token"] == "ghs_x"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_ordinary_5xx_still_retries_when_ratelimit_reset_is_present(monkeypatch):
    """Regression: GitHub stamps `X-RateLimit-Reset` on EVERY response as a
    quota gauge, not just on rate-limited ones. Reading it unconditionally
    made an ordinary 503 look "rate limited until the top of the hour", trip
    the fail-fast ceiling, and get ZERO retries — disabling 5xx resilience in
    the single most common case it exists for."""
    import time as _time

    from infrastructure import github_client as gh

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())
    calls = {"n": 0}
    # A perfectly healthy quota window that happens to reset in an hour.
    reset = str(_time.time() + 3600)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        headers = {"X-RateLimit-Reset": reset, "X-RateLimit-Remaining": "4999"}
        if calls["n"] == 1:
            return httpx.Response(503, headers=headers, json={"message": "unavailable"})
        return httpx.Response(200, headers=headers, json={"number": 1})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    assert await c.get_pull_request("acme", "repo", 1) == {"number": 1}
    assert calls["n"] == 2  # the 503 WAS retried
    assert sleeps == [1.0]  # exponential backoff, not the hour-away reset
    await c.aclose()


@pytest.mark.asyncio
async def test_5xx_retry_after_is_clamped_not_failed_fast(monkeypatch):
    """A 5xx `Retry-After` is a genuine backoff hint, so it is honored — but
    clamped to the ceiling rather than parking a worker, and never turned
    into a fail-fast the way a rate-limit hint is."""
    from infrastructure import github_client as gh
    from infrastructure.github_client import MAX_RETRY_DELAY_SECONDS

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "3600"}, json={"message": "unavailable"})
        return httpx.Response(200, json={"number": 1})

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    assert await c.get_pull_request("acme", "repo", 1) == {"number": 1}
    assert calls["n"] == 2
    assert sleeps == [float(MAX_RETRY_DELAY_SECONDS)]
    await c.aclose()


@pytest.mark.asyncio
async def test_rate_limited_403_reset_beyond_ceiling_still_fails_fast(monkeypatch):
    """The ceiling must keep working for real rate limits — only the
    always-present-header misread was wrong."""
    import time as _time

    from infrastructure import github_client as gh

    sleeps = []
    monkeypatch.setattr(gh.asyncio, "sleep", lambda d: sleeps.append(d) or _noop())
    calls = {"n": 0}
    reset = str(_time.time() + 3600)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            403,
            headers={"X-RateLimit-Reset": reset},
            json={"message": "API rate limit exceeded"},
        )

    c = GitHubClient(token="x")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_pull_request("acme", "repo", 1)
    assert calls["n"] == 1
    assert sleeps == []
    await c.aclose()
