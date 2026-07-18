"""Native executor todo mirroring (replaces the hand-authored steps[] system):
TodoWrite / todowrite / update_plan tool calls → normalized per-phase
snapshots on the thread."""

from domains.threads.services.thread_run import parse_native_todos


def test_claude_todowrite_parses():
    out = parse_native_todos(
        "TodoWrite",
        {
            "todos": [
                {"content": "Explore code", "status": "completed", "activeForm": "Exploring"},
                {"content": "Fix layout", "status": "in_progress"},
                {"content": "Run tests", "status": "pending"},
            ]
        },
    )
    assert [t["status"] for t in out] == ["completed", "in_progress", "pending"]
    assert out[0]["activeForm"] == "Exploring"


def test_json_string_input_parses():
    out = parse_native_todos(
        "TodoWrite", '{"todos": [{"content": "X", "status": "pending"}]}'
    )
    assert out == [{"content": "X", "status": "pending"}]


def test_codex_update_plan_maps_step_to_content():
    out = parse_native_todos(
        "update_plan",
        {"plan": [{"step": "Wire API", "status": "done"}, {"step": "Ship", "status": "pending"}]},
    )
    assert out[0] == {"content": "Wire API", "status": "completed"}
    assert out[1]["status"] == "pending"


def test_non_todo_tools_return_none():
    assert parse_native_todos("Read", {"file_path": "a.py"}) is None
    assert parse_native_todos("Bash", {"command": "ls"}) is None
    assert parse_native_todos("", None) is None


def test_malformed_input_is_tolerated():
    assert parse_native_todos("TodoWrite", "not json") is None
    assert parse_native_todos("TodoWrite", {"todos": "nope"}) is None
    assert parse_native_todos("TodoWrite", {"todos": [{"status": "pending"}]}) == []
