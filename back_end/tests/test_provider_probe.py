"""Health probes for the two harness kinds.

The opencode probe checks both halves of the harness: the CLI binary in the
container AND the configured endpoint. A hosted endpoint answering 401/403
without a key is REACHABLE (auth is applied at run time from the generated
config); only 5xx / transport errors count as down.
"""

from types import SimpleNamespace

import pytest

from domains.llm_providers.schemas import LLMProviderHealth
from domains.llm_providers.services.llm_provider_service import _probe

pytestmark = pytest.mark.asyncio


def _provider(kind, base_url=""):
    return SimpleNamespace(
        kind=kind, base_url=base_url, credential_secret="", api_key_env=""
    )


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    status_code = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return _FakeResponse(self.status_code)


async def test_claude_probe_checks_the_cli_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    status, detail = await _probe(_provider("claude_subscription"))
    assert status is LLMProviderHealth.OK

    monkeypatch.setattr("shutil.which", lambda b: None)
    status, _ = await _probe(_provider("claude_subscription"))
    assert status is LLMProviderHealth.UNREACHABLE


async def test_opencode_probe_needs_binary_and_base_url(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: None)
    status, detail = await _probe(_provider("opencode", base_url="http://x/v1"))
    assert status is LLMProviderHealth.UNREACHABLE
    assert "not on PATH" in detail

    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/opencode")
    status, detail = await _probe(_provider("opencode"))
    assert status is LLMProviderHealth.UNREACHABLE
    assert "base_url" in detail


@pytest.mark.parametrize("code", [200, 401, 403])
async def test_opencode_probe_counts_auth_challenges_as_reachable(monkeypatch, code):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/opencode")
    _FakeClient.status_code = code
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    status, detail = await _probe(_provider("opencode", base_url="http://host/v1"))
    assert status is LLMProviderHealth.OK
    assert str(code) in detail


async def test_opencode_probe_5xx_and_transport_errors_are_down(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/opencode")
    _FakeClient.status_code = 503
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    status, _ = await _probe(_provider("opencode", base_url="http://host/v1"))
    assert status is LLMProviderHealth.UNREACHABLE

    class _Boom(_FakeClient):
        async def get(self, url):
            raise OSError("connection refused")

    monkeypatch.setattr("httpx.AsyncClient", _Boom)
    status, detail = await _probe(_provider("opencode", base_url="http://host/v1"))
    assert status is LLMProviderHealth.UNREACHABLE
    assert "connection refused" in detail
