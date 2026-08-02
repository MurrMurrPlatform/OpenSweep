"""Coverage derived from what a run actually opened.

The claim side was never checkable: an agent that read one file of a 35-page
scope produced the same stamp as one that read all 35, and a run that reported
nothing had the dispatch order written down as its result. These tests pin the
measurement that replaces the guessing — and, just as importantly, pin where
the measurement is honestly unavailable.
"""

import json

import pytest

from domains.checked.services.observed_coverage import (
    observed_paths,
    observed_paths_for_run,
    paths_from_tool_use,
)

ROOT = "/workspace/abc123"


def _tool_use(name, **inp):
    """A tool_use event as the stream translator writes it — note `input` is
    a JSON STRING, because preview_structured serializes it."""
    return {"type": "tool_use", "name": name, "input": json.dumps(inp)}


# ── What counts as evidence ──────────────────────────────────────────────


def test_reading_a_file_is_evidence():
    assert paths_from_tool_use("Read", json.dumps({"file_path": f"{ROOT}/a.py"}), ROOT) == [
        "a.py"
    ]


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_every_content_bearing_tool_counts(tool):
    event = json.dumps({"file_path": f"{ROOT}/src/x.py"})
    assert paths_from_tool_use(tool, event, ROOT) == ["src/x.py"]


def test_tool_names_are_matched_case_insensitively():
    assert paths_from_tool_use("read", json.dumps({"file_path": f"{ROOT}/a.py"}), ROOT)


@pytest.mark.parametrize("tool", ["Grep", "Glob", "Bash", "Task", "WebFetch"])
def test_searching_is_not_reading(tool):
    """A Grep over back_end/ would otherwise stamp the whole tree as examined
    — the exact overclaim this module exists to end. Searching a directory is
    how you decide what to read, not evidence that you read it."""
    event = json.dumps({"path": f"{ROOT}/back_end", "pattern": "def "})
    assert paths_from_tool_use(tool, event, ROOT) == []


# ── Workspace boundary ───────────────────────────────────────────────────


def test_paths_outside_the_workspace_are_dropped():
    """/etc/hosts is not repository coverage. Recording it under a mangled
    relative name would be worse than not recording it."""
    for outside in ("/etc/hosts", "/tmp/scratch.py", "/workspace/other-run/a.py"):
        assert paths_from_tool_use("Read", json.dumps({"file_path": outside}), ROOT) == []


def test_a_relative_path_is_taken_as_repo_relative():
    assert paths_from_tool_use("Read", json.dumps({"file_path": "back_end/a.py"}), ROOT) == [
        "back_end/a.py"
    ]


def test_the_workspace_root_itself_yields_nothing_rather_than_a_stray_entry():
    assert paths_from_tool_use("Read", json.dumps({"file_path": ROOT}), ROOT) == []


# ── Degradation ──────────────────────────────────────────────────────────


def test_an_unparseable_input_costs_one_path_not_a_crash():
    """preview_structured falls back to a non-JSON preview when the whole
    input exceeds its budget — a huge Write body."""
    assert paths_from_tool_use("Write", "{content: truncated…", ROOT) == []


def test_a_dict_input_is_accepted_too():
    assert paths_from_tool_use("Read", {"file_path": f"{ROOT}/a.py"}, ROOT) == ["a.py"]


def test_missing_and_non_string_paths_are_ignored():
    assert paths_from_tool_use("Read", json.dumps({}), ROOT) == []
    assert paths_from_tool_use("Read", json.dumps({"file_path": 42}), ROOT) == []


# ── Whole streams ────────────────────────────────────────────────────────


def test_observed_paths_dedupes_and_keeps_first_seen_order():
    events = [
        _tool_use("Read", file_path=f"{ROOT}/b.py"),
        {"type": "assistant_text", "text": "thinking"},
        _tool_use("Read", file_path=f"{ROOT}/a.py"),
        _tool_use("Read", file_path=f"{ROOT}/b.py"),
    ]
    assert observed_paths(events, workspace_root=ROOT) == ["b.py", "a.py"]


def test_non_tool_events_are_ignored():
    events = [
        {"type": "user_message", "text": "go"},
        {"type": "turn_end", "status": "success"},
        "not-a-dict",
    ]
    assert observed_paths(events, workspace_root=ROOT) == []


def test_an_opencode_run_yields_nothing_and_that_is_not_zero_coverage():
    """The opencode adapter emits no per-tool-call events at all — its stdout
    becomes assistant_text. The empty result here means "not measured", which
    is why coverage_source carries the discriminator rather than this list's
    emptiness."""
    events = [{"type": "assistant_text", "text": "Reading back_end/app.py..."}]
    assert observed_paths(events, workspace_root=ROOT) == []


# ── The on-disk stream ───────────────────────────────────────────────────


def test_reads_the_run_event_file_past_the_read_events_cap(tmp_path, monkeypatch):
    """run_events.read_events caps at 2000 — a deep audit blows through that
    while still reading files, and a cap here would under-report coverage."""
    path = tmp_path / "run1.events.jsonl"
    lines = []
    for i in range(2500):
        lines.append(json.dumps({"seq": i, "type": "assistant_text", "text": "x"}))
    lines.append(json.dumps(_tool_use("Read", file_path=f"{ROOT}/late.py") | {"seq": 2500}))
    path.write_text("\n".join(lines), encoding="utf-8")

    import domains.runs.services.run_events as run_events

    monkeypatch.setattr(run_events, "events_path", lambda uid: path)

    assert observed_paths_for_run("run1", ROOT) == ["late.py"]


def test_a_missing_event_file_is_empty_not_an_error(tmp_path, monkeypatch):
    import domains.runs.services.run_events as run_events

    monkeypatch.setattr(run_events, "events_path", lambda uid: tmp_path / "nope.jsonl")
    assert observed_paths_for_run("run1", ROOT) == []


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    path = tmp_path / "run1.events.jsonl"
    path.write_text(
        "\n".join(
            [
                "{ not json",
                json.dumps(_tool_use("Read", file_path=f"{ROOT}/a.py")),
                "",
            ]
        ),
        encoding="utf-8",
    )

    import domains.runs.services.run_events as run_events

    monkeypatch.setattr(run_events, "events_path", lambda uid: path)
    assert observed_paths_for_run("run1", ROOT) == ["a.py"]


def test_no_run_uid_and_no_workspace_are_both_empty():
    assert observed_paths_for_run("", ROOT) == []
    assert observed_paths([_tool_use("Read", file_path="/a.py")], workspace_root="") == []
