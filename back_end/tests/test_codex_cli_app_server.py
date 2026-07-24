import json, pytest
from unittest.mock import patch
pytestmark = pytest.mark.asyncio


def test_mcp_config_object_nests_the_overrides():
    from domains.executors.mcp_bridge import codex_mcp_config_object
    with patch("domains.executors.mcp_bridge.mcp_remote_args", return_value=["-y", "mcp-remote", "URL"]):
        cfg = codex_mcp_config_object(run_uid="r1", workspace_path="")
    assert cfg["mcp_servers"]["opensweep"]["command"] == "npx"
    assert cfg["mcp_servers"]["opensweep"]["args"] == ["-y", "mcp-remote", "URL"]


async def test_run_via_app_server_starts_thread_runs_turn_and_streams(monkeypatch):
    from domains.llm_providers.services import codex_cli
    from domains.llm_providers.services.codex_app_server import TurnResult
    from types import SimpleNamespace

    calls = {}
    class _Client:
        async def start_thread(self, *, cwd, config=None, **kw):
            calls["cwd"] = cwd; calls["config"] = config; return "thr_1"
        async def run_turn(self, *, thread_id, text, on_delta=None, **kw):
            if on_delta: on_delta("hel"); on_delta("lo")
            calls["thread_id"] = thread_id; calls["text"] = text
            return TurnResult(text="hello", usage={"input_tokens": 2})
    async def fake_acquire(provider): return _Client()
    monkeypatch.setattr(codex_cli.REGISTRY, "acquire", fake_acquire)
    monkeypatch.setattr(codex_cli, "codex_mcp_config_object",
                        lambda **kw: {"mcp_servers": {"opensweep": {}}})

    seen = []
    res = await codex_cli.run_via_app_server(
        SimpleNamespace(uid="p1", kind="codex_subscription", credential_revision=0, model=""),
        instruction="do it", working_dir="/ws", run_uid="r1", on_delta=seen.append,
    )
    assert res.text == "hello" and "".join(seen) == "hello"
    assert calls["cwd"] == "/ws" and calls["text"] == "do it"
    assert calls["config"] == {"mcp_servers": {"opensweep": {}}}
