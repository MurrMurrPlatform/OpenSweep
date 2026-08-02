"""Pure tests for the code-graph integration (KNOWLEDGE_V3_CODE_GRAPH.md)."""

from unittest.mock import patch

from infrastructure.code_graph import (
    CODE_GRAPH_PROMPT,
    code_graph_codex_overrides,
    code_graph_opencode_server,
    code_graph_server_config,
)


def test_server_config_confines_cache_and_root_to_workspace():
    with patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"):
        cfg = code_graph_server_config("/host/sandboxes/abc")
    assert cfg is not None
    assert cfg["command"] == "/usr/local/bin/codebase-memory-mcp"
    assert cfg["env"]["CBM_ALLOWED_ROOT"] == "/host/sandboxes/abc"
    assert cfg["env"]["CBM_CACHE_DIR"] == "/host/sandboxes/abc/.opensweep-code-graph"


def test_server_config_none_without_binary_or_workspace():
    with patch("infrastructure.code_graph.code_graph_binary", return_value=""):
        assert code_graph_server_config("/host/sandboxes/abc") is None
    with patch("infrastructure.code_graph.code_graph_binary", return_value="/x"):
        assert code_graph_server_config("") is None


def test_prompt_briefing_names_server_and_tools():
    assert "code-graph" in CODE_GRAPH_PROMPT
    for tool in (
        "search_graph",
        "trace_path",
        "query_graph",
        "get_architecture",
        "get_code_snippet",
        "search_code",
        "detect_changes",
    ):
        assert tool in CODE_GRAPH_PROMPT
    assert "pre-indexed" in CODE_GRAPH_PROMPT
    assert "FIRST" in CODE_GRAPH_PROMPT  # graph-before-grep directive


def test_claude_prompts_carry_the_briefing():
    from domains.executors.claude_code import _SYSTEM_PROMPT, _SYSTEM_PROMPT_WRITE

    assert CODE_GRAPH_PROMPT in _SYSTEM_PROMPT
    assert CODE_GRAPH_PROMPT in _SYSTEM_PROMPT_WRITE


def test_mcp_config_includes_code_graph_when_workspace_known(tmp_path):
    import json

    from domains.executors.mcp_bridge import write_claude_mcp_config

    with patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"):
        path = write_claude_mcp_config(
            run_uid="r1", scratch_root=str(tmp_path), workspace_path="/host/sandboxes/abc"
        )
        config = json.loads(open(path).read())
        assert "code-graph" in config["mcpServers"]
        assert "opensweep-platform" in config["mcpServers"]

        # A later turn writing the SAME config with the workspace still set
        # must keep the server (regression: follow-up turns stripped it).
        path2 = write_claude_mcp_config(
            run_uid="r1", scratch_root=str(tmp_path), workspace_path="/host/sandboxes/abc"
        )
        config2 = json.loads(open(path2).read())
        assert "code-graph" in config2["mcpServers"]


def test_opencode_server_entry_uses_opencode_shape():
    with patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"):
        entry = code_graph_opencode_server("/host/sandboxes/abc")
    assert entry == {
        "type": "local",
        "command": ["/usr/local/bin/codebase-memory-mcp"],
        "enabled": True,
        "environment": {
            "CBM_CACHE_DIR": "/host/sandboxes/abc/.opensweep-code-graph",
            "CBM_ALLOWED_ROOT": "/host/sandboxes/abc",
        },
    }
    with patch("infrastructure.code_graph.code_graph_binary", return_value=""):
        assert code_graph_opencode_server("/host/sandboxes/abc") is None


def test_codex_overrides_are_toml_config_pairs():
    with patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"):
        overrides = code_graph_codex_overrides("/host/sandboxes/abc")
    assert overrides[0] == 'mcp_servers.code-graph.command="/usr/local/bin/codebase-memory-mcp"'
    assert overrides[1] == (
        'mcp_servers.code-graph.env={CBM_CACHE_DIR = "/host/sandboxes/abc/.opensweep-code-graph", '
        'CBM_ALLOWED_ROOT = "/host/sandboxes/abc"}'
    )
    with patch("infrastructure.code_graph.code_graph_binary", return_value=""):
        assert code_graph_codex_overrides("/host/sandboxes/abc") == []


def test_codex_turn_argv_carries_config_overrides():
    from domains.runs.services.turn_cli import build_codex_turn_argv

    argv = build_codex_turn_argv(
        prompt="hi", model="gpt-5", config_overrides=["a=1", "b=2"]
    )
    assert argv[:2] == ["codex", "exec"]
    assert ["-c", "a=1"] == argv[argv.index("a=1") - 1 : argv.index("a=1") + 1]
    assert argv[-1] == "hi"
    # No overrides → unchanged shape.
    assert "-c" not in build_codex_turn_argv(prompt="hi")


def test_opencode_generated_config_registers_code_graph(tmp_path):
    import json
    from types import SimpleNamespace

    from domains.llm_providers.services.llm_executor import _prepare_opencode_config

    provider = SimpleNamespace(
        base_url="http://mlx:8080/v1",
        model="opensweep/some-model",
        label="test",
        uid="p1",
    )
    with (
        patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"),
        patch("domains.llm_providers.services.llm_executor.os.makedirs"),
        patch("domains.llm_providers.services.llm_executor.os.chmod"),
        patch("builtins.open"),
        patch("json.dump") as dumped,
    ):
        _prepare_opencode_config(provider, run_uid="r1", working_dir="/host/sandboxes/abc")
    payload = dumped.call_args[0][0]
    assert "code-graph" in payload["mcp"]
    assert "opensweep" in payload["mcp"]

    # Without a workspace the opensweep server still registers, the graph doesn't.
    with (
        patch("domains.llm_providers.services.llm_executor.os.makedirs"),
        patch("domains.llm_providers.services.llm_executor.os.chmod"),
        patch("builtins.open"),
        patch("json.dump") as dumped,
    ):
        _prepare_opencode_config(provider, run_uid="r1")
    payload = dumped.call_args[0][0]
    assert "code-graph" not in payload["mcp"]
    assert "opensweep" in payload["mcp"]


def test_codex_mcp_overrides_register_opensweep_and_graph():
    from domains.executors.mcp_bridge import codex_mcp_overrides

    with (
        patch("infrastructure.code_graph.code_graph_binary", return_value="/usr/local/bin/codebase-memory-mcp"),
        patch("domains.executors.mcp_bridge.mint_run_token", return_value="tok"),
    ):
        overrides = codex_mcp_overrides(run_uid="r1", workspace_path="/host/sandboxes/abc")
    assert overrides[0] == 'mcp_servers.opensweep.command="npx"'
    assert overrides[1].startswith("mcp_servers.opensweep.args=[")
    assert '"X-OpenSweep-Run-Uid: r1"' in overrides[1]
    assert '"X-OpenSweep-Auth: tok"' in overrides[1]
    assert any(o.startswith("mcp_servers.code-graph.command=") for o in overrides)

    # No run uid → no opensweep server; no binary → no graph.
    with patch("infrastructure.code_graph.code_graph_binary", return_value=""):
        assert codex_mcp_overrides(run_uid="", workspace_path="/host/sandboxes/abc") == []


def test_codex_dispatch_argv_gains_mcp_overrides_after_exec():
    from domains.llm_providers.services.codex_cli import with_mcp_overrides

    with patch(
        "domains.executors.mcp_bridge.codex_mcp_overrides",
        return_value=["a=1", "b=2"],
    ):
        argv = with_mcp_overrides(
            ["codex", "exec", "--json", "prompt"], run_uid="r1", working_dir="/w"
        )
        assert argv == ["codex", "exec", "-c", "a=1", "-c", "b=2", "--json", "prompt"]
        # Unrecognized template shape passes through untouched.
        assert with_mcp_overrides(["codex", "--json"], run_uid="r1", working_dir="/w") == [
            "codex",
            "--json",
        ]
