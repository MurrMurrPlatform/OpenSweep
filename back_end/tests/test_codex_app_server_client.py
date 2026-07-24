import sys
import pytest
from domains.llm_providers.services.codex_app_server import AppServerClient, AppServerError

pytestmark = pytest.mark.asyncio
_FAKE = [sys.executable, "tests/fixtures/fake_codex_app_server.py"]


async def test_initialize_handshake_returns_server_info():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        info = await c.initialize()
        assert info["platformOs"] == "test"
    finally:
        await c.close()


async def test_request_raises_appservererror_on_error_response():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        with pytest.raises(AppServerError) as exc:
            await c.request("boom/error")
        assert exc.value.code == -32000 and "boom" in exc.value.message
    finally:
        await c.close()
