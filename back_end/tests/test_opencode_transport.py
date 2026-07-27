"""OpenCode session continuity + the `--format json` event transport.

Before this, an opencode run was a one-shot `opencode run -m <model> <prompt>`
whose plain-text stdout the platform pasted back into a FRESH process when it
needed a continuation pass — 8 000 characters of transcript tail standing in for
a session the CLI supposedly did not have. It does have one: `opencode run
-s <session id>` restores the real conversation across process boundaries, and
`--format json` puts the session handle on every event alongside the token and
cost telemetry the platform previously had none of for opencode.

What is pinned here:
  - the session id is captured from the FIRST event (a stream cut off after one
    line still yields a resumable handle) and lands on `Run.cli_session_id`;
  - `-s` is passed on the continuation pass and NOT on the first one, and
    `-c/--continue` is never passed at all — it resolves per DIRECTORY, and
    sandbox clones live at fresh paths, so `-c` would silently open a new
    session and lose pass one entirely;
  - each event type parses to the transcript events the UI already renders,
    including a failed tool (`state.status == "error"`, which carries `error`
    and no `output`);
  - tokens and cost are SUMMED across steps and passes, and their absence is
    never an error — opencode bug #26855 lets `--format json` exit before the
    final `step_finish`, so process exit is the only completion signal;
  - the tool_calls envelope, which arrives JSON-ESCAPED inside a `text` event
    and is therefore invisible in the raw JSONL, still gets extracted;
  - codex's transcript-tail continuation and JSONL feeder are untouched.

Every JSONL fixture below is a verbatim capture from opencode 1.15.10, not a
hand-written approximation of the schema.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from domains.executors import cli_tracking
from domains.executors._shared import extract_envelope
from domains.executors.base import DispatchRequest
from domains.llm_providers.schemas import default_cli_template
from domains.llm_providers.services import llm_executor
from domains.llm_providers.services.llm_executor import (
    LLMInvocation,
    OpenCodeEventReducer,
    apply_opencode_events,
    opencode_json_format_on,
    with_opencode_transport_flags,
)
from domains.runs.schemas import ExecutionMode, RunStatus

# ── captured fixtures (opencode 1.15.10, `--format json`) ─────────────────

SESSION = "ses_0602cdeb1ffe84fFF9LTZKmPGZ"

# A plain text run: step_start → text → step_finish.
SIMPLE_RUN = (
    '{"type":"step_start","timestamp":1785092843596,"sessionID":"' + SESSION + '",'
    '"part":{"id":"prt_a","messageID":"msg_a","sessionID":"' + SESSION + '",'
    '"snapshot":"f9b2406","type":"step-start"}}\n'
    '{"type":"text","timestamp":1785092844718,"sessionID":"' + SESSION + '",'
    '"part":{"id":"prt_b","messageID":"msg_a","sessionID":"' + SESSION + '",'
    '"type":"text","text":"PONG","time":{"start":1,"end":2}}}\n'
    '{"type":"step_finish","timestamp":1785092844804,"sessionID":"' + SESSION + '",'
    '"part":{"id":"prt_c","reason":"stop","snapshot":"d3bbdad","messageID":"msg_a",'
    '"sessionID":"' + SESSION + '","type":"step-finish",'
    '"tokens":{"total":28331,"input":28314,"output":3,"reasoning":14,'
    '"cache":{"write":0,"read":0}},"cost":0.25}}\n'
)

# A two-step run containing a completed `read` tool call. Note that the tool
# call and its result arrive as ONE event, and that `total` is each STEP's own
# arithmetic (input+output+reasoning+cache.read), not a running tally.
TOOL_RUN = (
    '{"type":"step_start","timestamp":1785092873636,"sessionID":"ses_tool",'
    '"part":{"id":"prt_1","messageID":"msg_1","sessionID":"ses_tool",'
    '"snapshot":"54c9e87","type":"step-start"}}\n'
    '{"type":"tool_use","timestamp":1785092875389,"sessionID":"ses_tool",'
    '"part":{"type":"tool","tool":"read","callID":"call_00_m0A4",'
    '"state":{"status":"completed","input":{"filePath":"/tmp/a.txt"},'
    '"output":"<path>/tmp/a.txt</path>\\n<content>\\n1: hello\\n</content>",'
    '"metadata":{"preview":"hello"},"title":"a.txt","time":{"start":1,"end":2}},'
    '"id":"prt_2","sessionID":"ses_tool","messageID":"msg_1"}}\n'
    '{"type":"step_finish","timestamp":1785092875526,"sessionID":"ses_tool",'
    '"part":{"id":"prt_3","reason":"tool-calls","messageID":"msg_1",'
    '"sessionID":"ses_tool","type":"step-finish",'
    '"tokens":{"total":28446,"input":28320,"output":99,"reasoning":27,'
    '"cache":{"write":0,"read":0}},"cost":0.5}}\n'
    '{"type":"step_start","timestamp":1785092876654,"sessionID":"ses_tool",'
    '"part":{"id":"prt_4","messageID":"msg_2","sessionID":"ses_tool",'
    '"snapshot":"8cfead1","type":"step-start"}}\n'
    '{"type":"text","timestamp":1785092877528,"sessionID":"ses_tool",'
    '"part":{"id":"prt_5","messageID":"msg_2","sessionID":"ses_tool",'
    '"type":"text","text":"hello","time":{"start":1,"end":2}}}\n'
    '{"type":"step_finish","timestamp":1785092877610,"sessionID":"ses_tool",'
    '"part":{"id":"prt_6","reason":"stop","messageID":"msg_2",'
    '"sessionID":"ses_tool","type":"step-finish",'
    '"tokens":{"total":28552,"input":128,"output":2,"reasoning":6,'
    '"cache":{"write":0,"read":28416}},"cost":0.25}}\n'
)

# A failing tool: status="error", an `error` string, and NO `output` key.
FAILED_TOOL_EVENT = (
    '{"type":"tool_use","timestamp":1785093162498,"sessionID":"ses_err",'
    '"part":{"type":"tool","tool":"read","callID":"call_00_5ydf",'
    '"state":{"status":"error","input":{"filePath":"/definitely/does/not/exist-xyz.txt"},'
    '"error":"File not found: /definitely/does/not/exist-xyz.txt",'
    '"time":{"start":1,"end":2}},'
    '"id":"prt_e","sessionID":"ses_err","messageID":"msg_e"}}\n'
)

# `opencode run -m <a model that does not exist>` — errors arrive as events on
# STDOUT, not on stderr.
ERROR_RUN = (
    '{"type":"error","timestamp":1785093330329,"sessionID":"ses_bad",'
    '"error":{"name":"UnknownError","data":{"message":"Model not found: x/y."}}}\n'
    '{"type":"error","timestamp":1785093330329,"sessionID":"ses_bad",'
    '"error":{"name":"UnknownError","data":{"message":"Unexpected server error.",'
    '"ref":"err_02d9449a"}}}\n'
)

# The final tool_calls envelope as opencode actually delivers it: a fenced JSON
# block, JSON-escaped, inside the `text` field of a `text` event.
ENVELOPE_RUN = (
    '{"type":"step_start","timestamp":1,"sessionID":"ses_env",'
    '"part":{"id":"prt_x","type":"step-start"}}\n'
    '{"type":"text","timestamp":2,"sessionID":"ses_env","part":{"id":"prt_y",'
    '"type":"text","text":"```json\\n{\\"tool_calls\\": [{\\"tool\\": \\"complete_run\\", '
    '\\"args\\": {\\"summary\\": \\"done\\"}}], \\"summary\\": \\"ok\\"}\\n```"}}\n'
    '{"type":"step_finish","timestamp":3,"sessionID":"ses_env","part":{"id":"prt_z",'
    '"type":"step-finish","tokens":{"total":10,"input":8,"output":2,"reasoning":0,'
    '"cache":{"write":0,"read":0}},"cost":0}}\n'
)

OPENCODE_TEMPLATE = default_cli_template("opencode")


def _reduce(text: str) -> tuple[OpenCodeEventReducer, list[dict]]:
    """Feed a whole stream line by line; return (reducer, emitted events)."""
    reducer = OpenCodeEventReducer()
    events: list[dict] = []
    for line in text.splitlines():
        events.extend(reducer.feed_line(line))
    return reducer, events


# ── argv: which flags OpenSweep adds, and which it must never add ─────────


def test_first_pass_argv_asks_for_json_and_carries_no_session():
    """The first pass has no session to resume; it is the one that MINTS the
    session id, which only `--format json` surfaces."""
    argv = with_opencode_transport_flags(
        ["opencode", "run", "-m", "opensweep/m", "do the thing"]
    )
    assert argv[:5] == ["opencode", "run", "-m", "opensweep/m", "do the thing"]
    assert argv[-2:] == ["--format", "json"]
    assert "-s" not in argv and "--session" not in argv


def test_continuation_argv_passes_the_session_id():
    argv = with_opencode_transport_flags(
        ["opencode", "run", "-m", "opensweep/m", "continue"], session_id=SESSION
    )
    assert argv[argv.index("-s") + 1] == SESSION
    assert opencode_json_format_on(argv) is True


@pytest.mark.parametrize("session_id", ["", SESSION])
def test_continue_flag_is_never_used(session_id):
    """`-c/--continue` resolves "the last session" PER DIRECTORY. OpenSweep runs
    each pass inside a disposable sandbox clone at a fresh path, so `-c` would
    quietly start a brand-new session and the continuation would lose every
    thing pass one learned. Only an explicit `-s <id>` survives that."""
    argv = with_opencode_transport_flags(
        ["opencode", "run", "-m", "opensweep/m", "go"], session_id=session_id
    )
    assert "-c" not in argv
    assert "--continue" not in argv


def test_operator_format_choice_wins():
    """The template is a user-editable field. Someone who deliberately pinned
    `--format default` (to eyeball opencode's own rendering, say) must not have
    it overwritten — they lose the telemetry, which is their call."""
    argv = with_opencode_transport_flags(
        ["opencode", "run", "--format", "default", "go"], session_id=SESSION
    )
    assert argv.count("--format") == 1
    assert opencode_json_format_on(argv) is False


def test_operator_session_flag_wins():
    argv = with_opencode_transport_flags(
        ["opencode", "run", "--session", "ses_theirs", "go"], session_id=SESSION
    )
    assert "ses_theirs" in argv
    assert SESSION not in argv


def test_title_names_the_session_after_the_run():
    argv = with_opencode_transport_flags(["opencode", "run", "go"], title="opensweep run r1")
    assert argv[argv.index("--title") + 1] == "opensweep run r1"


def test_operator_title_wins():
    argv = with_opencode_transport_flags(
        ["opencode", "run", "--title", "mine", "go"], title="opensweep run r1"
    )
    assert argv.count("--title") == 1
    assert "opensweep run r1" not in argv


def test_json_format_detection_tolerates_a_dangling_flag():
    """`--format` with nothing after it is a malformed operator template, not a
    reason to raise inside the transport."""
    assert opencode_json_format_on(["opencode", "run", "go", "--format"]) is False
    assert opencode_json_format_on(["opencode", "run", "go"]) is False


def test_default_template_stays_free_of_transport_flags():
    """The flags are injected at dispatch precisely so a customised template
    keeps working. Baking them in here would defeat that AND make this the
    second place they live."""
    assert OPENCODE_TEMPLATE == "opencode run -m {{model_q}} {{instruction_q}}"
    for flag in ("--format", "-s", "--session", "-c", "--continue", "--title"):
        assert flag not in OPENCODE_TEMPLATE


# ── the event reducer ─────────────────────────────────────────────────────


def test_session_id_comes_off_the_very_first_event():
    """Captured fact: `sessionID` rides on EVERY event including step_start. A
    run killed on the wall ceiling after one line still yields a handle the
    next pass can resume."""
    reducer = OpenCodeEventReducer()
    reducer.feed_line(SIMPLE_RUN.splitlines()[0])
    assert reducer.session_id == SESSION
    assert reducer.tokens == {}  # no step_finish yet — and that is fine


def test_text_events_become_assistant_deltas():
    reducer, events = _reduce(SIMPLE_RUN)
    assert events == [{"type": "assistant_text", "text": "PONG"}]
    assert reducer.text == "PONG"


def test_tool_use_becomes_a_call_and_a_result():
    _, events = _reduce(TOOL_RUN)
    call = next(e for e in events if e["type"] == "tool_use")
    result = next(e for e in events if e["type"] == "tool_result")
    assert call["name"] == "read"
    assert call["input"] == {"filePath": "/tmp/a.txt"}
    assert result["name"] == "read"
    assert result["is_error"] is False
    assert "1: hello" in result["output"]


def test_a_failed_tool_is_reported_as_an_error():
    """`state.status == "error"` carries `error` and NO `output`. Reading only
    `output` would render a failed tool as a successful one with a blank
    result — the agent's own view of what happened and the operator's would
    then disagree."""
    _, events = _reduce(FAILED_TOOL_EVENT)
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] is True
    assert "File not found" in result["output"]


def test_step_finish_tokens_and_cost_are_summed_across_steps():
    """Each step is a separately billed API call. Taking the LAST step would
    report this two-step run as 28 552 tokens and $0.25 when it actually cost
    56 998 and $0.75 — and tool-using runs, which have the most steps, are
    exactly the expensive ones."""
    reducer, _ = _reduce(TOOL_RUN)
    assert reducer.step_finishes == 2
    assert reducer.tokens["total"] == 28446 + 28552
    assert reducer.tokens["input"] == 28320 + 128
    assert reducer.tokens["output"] == 99 + 2
    assert reducer.tokens["reasoning"] == 27 + 6
    assert reducer.tokens["cache_read"] == 28416
    assert reducer.cost == pytest.approx(0.75)


def test_a_stream_that_never_emits_step_finish_still_parses():
    """opencode bug #26855: `--format json` can exit BEFORE the final
    step_finish. Losing the token bill is annoying; failing an otherwise
    successful run over it would be a self-inflicted outage, so nothing here
    treats the absence as an error."""
    truncated = "\n".join(SIMPLE_RUN.splitlines()[:2])
    reducer, events = _reduce(truncated)
    assert reducer.session_id == SESSION
    assert reducer.text == "PONG"
    assert reducer.tokens == {}
    assert reducer.cost == 0.0
    assert reducer.step_finishes == 0
    assert events == [{"type": "assistant_text", "text": "PONG"}]


def test_error_events_reach_both_the_transcript_and_the_text():
    """opencode reports model/server failures as `error` events on STDOUT.
    `detect_quota_exhaustion` greps the output tail, so a rate limit that only
    existed as a JSON line it cannot read would FAIL the run instead of pausing
    it for retry."""
    reducer, events = _reduce(ERROR_RUN)
    assert [e["type"] for e in events] == ["error", "error"]
    assert events[0]["detail"] == "Model not found: x/y."
    assert "Model not found" in reducer.text
    assert "Unexpected server error" in reducer.text


def test_quota_wording_in_an_error_event_still_triggers_the_pause():
    from domains.executors.quota import detect_quota_exhaustion

    stream = (
        '{"type":"error","timestamp":1,"sessionID":"ses_q",'
        '"error":{"name":"RateLimit","data":{"message":"You have reached your usage limit."}}}\n'
    )
    reducer, _ = _reduce(stream)
    assert detect_quota_exhaustion(1, reducer.text, "") is True


def test_non_json_lines_pass_through_untouched():
    """A warning or stack trace printed straight to stdout is not an event and
    must not be silently dropped — it is often the only explanation of what
    went wrong."""
    reducer, events = _reduce("npm WARN something\n" + SIMPLE_RUN)
    assert events[0] == {"type": "assistant_text", "text": "npm WARN something"}
    assert reducer.text.startswith("npm WARN something\n")
    assert reducer.saw_json is True


def test_a_stream_with_no_events_at_all_is_not_mistaken_for_one():
    reducer, _ = _reduce("opencode: command failed\n")
    assert reducer.saw_json is False


def test_repeated_text_parts_emit_only_the_new_suffix():
    """1.15.10 sends one complete `text` event per message part, but a build
    that streamed partial parts would otherwise re-emit the whole message on
    every update — a 1 500-character answer repeated once per token."""
    part = '{{"type":"text","timestamp":1,"sessionID":"ses_p","part":{{"id":"prt_p",' \
           '"type":"text","text":"{text}"}}}}'
    reducer = OpenCodeEventReducer()
    first = reducer.feed_line(part.format(text="Hel"))
    second = reducer.feed_line(part.format(text="Hello"))
    assert first == [{"type": "assistant_text", "text": "Hel"}]
    assert second == [{"type": "assistant_text", "text": "lo"}]
    assert reducer.text == "Hello"


# ── streaming: running totals, buffering, flush ───────────────────────────


def test_feed_total_consumes_running_totals_like_the_codex_feeder():
    """The run path's on_chunk delivers the cumulative stdout on every tick,
    not the delta. Re-parsing the total each time would replay the whole
    transcript into the events file on every chunk."""
    reducer = OpenCodeEventReducer()
    lines = SIMPLE_RUN.splitlines(keepends=True)
    seen: list[dict] = []
    total = ""
    for line in lines:
        total += line
        seen.extend(reducer.feed_total(total))
    assert seen == [{"type": "assistant_text", "text": "PONG"}]
    assert reducer.tokens["total"] == 28331


def test_feed_total_buffers_a_half_arrived_line():
    reducer = OpenCodeEventReducer()
    line = SIMPLE_RUN.splitlines(keepends=True)[1]
    half = len(line) // 2
    assert reducer.feed_total(line[:half]) == []
    assert reducer.feed_total(line) == [{"type": "assistant_text", "text": "PONG"}]


def test_flush_recovers_a_last_line_with_no_trailing_newline():
    """The final event is not guaranteed to be newline-terminated, and it is
    the one carrying the whole run's token bill."""
    reducer = OpenCodeEventReducer()
    reducer.feed_total(SIMPLE_RUN.rstrip("\n"))
    assert reducer.tokens == {}  # still buffered
    reducer.flush()
    assert reducer.tokens["total"] == 28331
    assert reducer.cost == pytest.approx(0.25)


def test_flush_on_an_empty_buffer_is_a_no_op():
    reducer = OpenCodeEventReducer()
    reducer.feed_total(SIMPLE_RUN)
    assert reducer.flush() == []


# ── apply_opencode_events: what the rest of the platform sees ─────────────


def test_apply_swaps_raw_output_for_the_reconstructed_transcript():
    """`raw_output` is what every existing caller already treats as the model's
    answer — turn_service returns it verbatim as a chat reply. Leaving JSONL
    there would show users raw events in the conversation."""
    inv = LLMInvocation(raw_output=SIMPLE_RUN)
    apply_opencode_events(inv)
    assert inv.raw_output == "PONG"
    assert inv.extra["opencode_raw_events"] == SIMPLE_RUN
    assert inv.extra["opencode"]["session_id"] == SESSION
    assert inv.extra["opencode"]["tokens"]["total"] == 28331
    assert inv.extra["opencode"]["cost"] == pytest.approx(0.25)
    assert inv.extra["opencode"]["steps"] == 1


def test_apply_leaves_a_non_json_stream_byte_identical():
    """A crash before the first event, or an operator template we misjudged:
    the transcript must degrade to exactly the pre-JSON behaviour."""
    inv = LLMInvocation(raw_output="opencode: something went wrong\n")
    apply_opencode_events(inv)
    assert inv.raw_output == "opencode: something went wrong\n"
    assert inv.extra == {}


def test_the_envelope_survives_the_jsonl_transport():
    """THE regression this guards: the tool_calls envelope arrives JSON-ESCAPED
    inside a `text` event, so a brace scan over the raw JSONL finds only event
    objects and returns nothing. Every finding, memory and complete_run in an
    opencode run rides in that envelope."""
    assert extract_envelope(ENVELOPE_RUN) is None  # the trap, over raw JSONL

    inv = LLMInvocation(raw_output=ENVELOPE_RUN)
    apply_opencode_events(inv)
    envelope = extract_envelope(inv.raw_output)
    assert envelope is not None
    assert cli_tracking.envelope_has_complete_run(envelope) is True
    assert envelope["summary"] == "ok"


def test_apply_survives_a_missing_step_finish(monkeypatch):
    inv = LLMInvocation(raw_output="\n".join(SIMPLE_RUN.splitlines()[:2]))
    apply_opencode_events(inv)
    assert inv.extra["opencode"]["session_id"] == SESSION
    assert inv.extra["opencode"]["tokens"] == {}
    assert inv.extra["opencode"]["cost"] == 0


# ── the CLI transport end to end (argv actually handed to the subprocess) ──


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._lines = data.splitlines(keepends=True)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(b"")
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


def _spy_subprocess(monkeypatch, stdout: bytes):
    seen: dict = {}

    async def _exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env") or {}
        return _FakeProc(stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    return seen


def _opencode_provider():
    return SimpleNamespace(
        uid="p1",
        kind="opencode",
        label="opencode",
        model="opensweep/Qwen3.6-35B-A3B-4bit",
        base_url="http://host.docker.internal:2345/v1",
        cli_command_template=OPENCODE_TEMPLATE,
        extra_args="",
        api_key_env="",
    )


@pytest.mark.asyncio
async def test_invoke_renders_the_real_argv_and_reduces_the_stream(monkeypatch):
    seen = _spy_subprocess(monkeypatch, SIMPLE_RUN.encode())

    inv = await llm_executor.invoke(
        _opencode_provider(),
        system_prompt="sys",
        instruction="do the thing",
        timeout_seconds=None,
        run_uid="run-1",
    )

    argv = seen["argv"]
    assert argv[:2] == ["opencode", "run"]
    assert "do the thing" in argv
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--title") + 1] == "opensweep run run-1"
    assert "-s" not in argv and "-c" not in argv and "--continue" not in argv
    # The reduced transcript, not the wire format.
    assert inv.raw_output == "PONG"
    assert inv.extra["opencode"]["session_id"] == SESSION


@pytest.mark.asyncio
async def test_invoke_resumes_the_session_when_the_caller_has_one(monkeypatch):
    seen = _spy_subprocess(monkeypatch, SIMPLE_RUN.encode())

    await llm_executor.invoke(
        _opencode_provider(),
        system_prompt="sys",
        instruction="continue",
        timeout_seconds=None,
        run_uid="run-1",
        cli_session_id=SESSION,
    )

    argv = seen["argv"]
    assert argv[argv.index("-s") + 1] == SESSION
    # Renaming a session that already has a title buys nothing.
    assert "--title" not in argv


@pytest.mark.asyncio
async def test_a_non_opencode_kind_gets_no_opencode_flags(monkeypatch):
    """codex is driven through `exec --json` and its own `-c` overrides. An
    `--format json` leaking onto its argv would be an unknown flag."""
    seen = _spy_subprocess(monkeypatch, b'{"type":"thread.started"}\n')
    provider = SimpleNamespace(
        uid="p2",
        kind="codex_subscription",
        label="codex",
        model="",
        base_url="",
        cli_command_template="codex exec --json {{instruction_q}}",
        extra_args="",
        api_key_env="",
    )

    monkeypatch.setattr(
        llm_executor.codex_cli, "with_mcp_overrides", lambda argv, **kw: argv
    )
    monkeypatch.setattr(llm_executor.codex_cli, "with_sandbox_bypass", lambda argv: argv)

    inv = await llm_executor.invoke(
        provider, system_prompt="sys", instruction="go", timeout_seconds=None,
        run_uid="run-2", cli_session_id=SESSION,
    )

    assert "--format" not in seen["argv"]
    assert "-s" not in seen["argv"]
    # codex's raw JSONL is untouched — its own feeder parses it downstream.
    assert inv.raw_output == '{"type":"thread.started"}\n'
    assert "opencode" not in inv.extra


# ── the continuation payload ──────────────────────────────────────────────


def test_continuation_with_a_session_id_sends_the_nudge_alone():
    """The CLI reloads the real conversation, so re-pasting a transcript tail
    would spend context re-telling the model something it already has."""
    prompt, session = cli_tracking._continuation_payload(
        nudge="Continue the run", transcript_tail="X" * 20_000, session_id=SESSION
    )
    assert prompt == "Continue the run"
    assert session == SESSION
    assert "X" not in prompt


def test_continuation_without_a_session_id_falls_back_to_the_tail():
    """No session handle is a degraded continuation, not a crash."""
    prompt, session = cli_tracking._continuation_payload(
        nudge="Continue the run", transcript_tail="Y" * 20_000, session_id=""
    )
    assert session == ""
    assert "Continue the run" in prompt
    assert "Y" * cli_tracking.CONTINUATION_TAIL_CAP in prompt
    assert "Y" * (cli_tracking.CONTINUATION_TAIL_CAP + 1) not in prompt
    assert "no session resume" in prompt


# ── usage accumulation across passes ──────────────────────────────────────


def _inv_with(tokens, cost, steps=1, session_id=SESSION):
    return SimpleNamespace(
        extra={
            "opencode": {
                "session_id": session_id,
                "tokens": tokens,
                "cost": cost,
                "steps": steps,
            }
        }
    )


def test_usage_is_summed_across_passes():
    """Same reasoning as summing across steps: a continuation pass is more
    billed API calls, not a restatement of the first pass's bill."""
    total = cli_tracking._merge_opencode_usage({}, _inv_with({"total": 10, "input": 8}, 0.5))
    total = cli_tracking._merge_opencode_usage(
        total, _inv_with({"total": 4, "input": 3}, 0.25)
    )
    assert total["tokens"] == {"total": 14, "input": 11}
    assert total["cost"] == pytest.approx(0.75)
    assert total["steps"] == 2
    assert total["session_id"] == SESSION


def test_usage_merge_ignores_an_invocation_that_reported_nothing():
    """#26855 again — a pass with no step_finish contributes nothing and must
    not zero out or corrupt what earlier passes reported."""
    total = cli_tracking._merge_opencode_usage({}, _inv_with({"total": 10}, 0.5))
    total = cli_tracking._merge_opencode_usage(total, SimpleNamespace(extra={}))
    total = cli_tracking._merge_opencode_usage(total, SimpleNamespace())
    assert total["tokens"] == {"total": 10}
    assert total["cost"] == pytest.approx(0.5)


def test_raw_event_stream_prefers_the_wire_format_for_the_artifact():
    """When the reducer misreads an event, the raw artifact is the only place
    left holding the bytes it misread."""
    inv = SimpleNamespace(
        raw_output="PONG", extra={"opencode_raw_events": SIMPLE_RUN}
    )
    assert cli_tracking._raw_event_stream(inv) == SIMPLE_RUN
    assert cli_tracking._raw_event_stream(SimpleNamespace(raw_output="plain", extra={})) == "plain"


# ── adapter dispatch: first pass vs continuation ──────────────────────────


def _req(**overrides):
    base = dict(
        run_uid="r-oc",
        scheduled_agent_uid="",
        repository_uid="repo1",
        repository_local_path="",
        intent="find things",
        mode=ExecutionMode.ANALYZE_ONLY,
    )
    base.update(overrides)
    return DispatchRequest(**base)


async def _noop(*a, **k):
    return None


def _patch_adapter(monkeypatch, provider_kind="opencode"):
    """Isolate the adapter from the graph: provider resolution, MCP completion
    lookup, the area gate and session persistence all become no-ops."""
    provider = SimpleNamespace(
        uid="p1",
        kind=provider_kind,
        label="p",
        base_url="http://host.docker.internal:2345/v1",
        model="opensweep/m",
        cli_command_template="",
        extra_args="",
        api_key_env="",
    )

    async def _resolve(*a, **k):
        return provider

    async def _not_completed(*a, **k):
        return False

    async def _no_gap(*a, **k):
        return ""

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _not_completed)
    monkeypatch.setattr(cli_tracking, "area_partition_nudge", _no_gap)
    monkeypatch.setattr(cli_tracking, "record_input", _noop)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "_persist_opencode_session", _noop)
    return provider


def _fake_invocations(monkeypatch, outputs):
    """Record every invocation's kwargs; serve `outputs` in order."""
    calls: list[dict] = []
    remaining = list(outputs)

    async def _invoke(provider, **kw):
        calls.append(kw)
        raw = remaining.pop(0) if remaining else ""
        inv = LLMInvocation(raw_output=raw, exit_code=0, transport="cli")
        apply_opencode_events(inv)
        return inv

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)
    return calls


@pytest.mark.asyncio
async def test_the_first_pass_carries_no_session_and_the_continuation_does(monkeypatch):
    """The whole point of the change, at the level the adapter controls it."""
    _patch_adapter(monkeypatch)
    # Pass one stops without complete_run; pass two finishes.
    calls = _fake_invocations(monkeypatch, [SIMPLE_RUN, ENVELOPE_RUN])

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 2
    assert calls[0]["cli_session_id"] == ""
    assert calls[1]["cli_session_id"] == SESSION
    # And the continuation prompt is the nudge, NOT 8 000 characters of tail.
    assert calls[1]["instruction"] == cli_tracking._CONTINUATION_NUDGE_TRACKING
    assert "no session resume" not in calls[1]["instruction"]
    assert result.usage["continuation_pass"] is True


@pytest.mark.asyncio
async def test_a_pass_with_no_session_id_falls_back_to_the_transcript_tail(monkeypatch):
    """Graceful degradation, not a crash: an opencode build or template that
    yields no session id gets exactly the behaviour that shipped before."""
    _patch_adapter(monkeypatch)
    calls = _fake_invocations(monkeypatch, ["I looked at some files.\n", ENVELOPE_RUN])

    await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert len(calls) == 2
    assert calls[1]["cli_session_id"] == ""
    assert "I looked at some files." in calls[1]["instruction"]
    assert "no session resume" in calls[1]["instruction"]


@pytest.mark.asyncio
async def test_the_session_id_is_persisted_before_the_continuation(monkeypatch):
    """Persisted early so a worker that dies between the passes still leaves a
    resumable run behind."""
    _patch_adapter(monkeypatch)
    _fake_invocations(monkeypatch, [SIMPLE_RUN, ENVELOPE_RUN])
    persisted: list[tuple] = []

    async def _persist(run_uid, session_id):
        persisted.append((run_uid, session_id))

    monkeypatch.setattr(cli_tracking, "_persist_opencode_session", _persist)

    await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert persisted == [("r-oc", SESSION)]


@pytest.mark.asyncio
async def test_tokens_and_cost_land_in_the_run_usage(monkeypatch):
    """Real provider telemetry the platform had none of for opencode."""
    _patch_adapter(monkeypatch)
    _fake_invocations(monkeypatch, [TOOL_RUN, ENVELOPE_RUN])

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    usage = result.usage["cli_usage"]
    assert usage["tokens"]["total"] == 28446 + 28552 + 10
    assert usage["cost"] == pytest.approx(0.75)
    assert usage["steps"] == 3
    assert usage["session_id"] == "ses_env"


@pytest.mark.asyncio
async def test_a_run_whose_step_finish_never_arrived_still_succeeds(monkeypatch):
    """opencode #26855 head on: process exit is the completion signal, so a
    stream that stops after the envelope is a SUCCESSFUL run with no token
    telemetry — not a failure, and not a retry."""
    _patch_adapter(monkeypatch)
    truncated = "\n".join(ENVELOPE_RUN.splitlines()[:2]) + "\n"
    _fake_invocations(monkeypatch, [truncated])

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert result.status is RunStatus.AWAITING_INPUT
    assert result.error == ""
    assert result.parse_status == "ok"
    assert result.usage["tool_calls"] == 1
    assert result.usage["cli_usage"]["tokens"] == {}
    assert result.usage["cli_usage"]["cost"] == 0


@pytest.mark.asyncio
async def test_the_envelope_is_executed_from_a_jsonl_transcript(monkeypatch):
    """End to end: complete_run reached the dispatcher even though it was
    JSON-escaped inside a `text` event."""
    _patch_adapter(monkeypatch)
    _fake_invocations(monkeypatch, [ENVELOPE_RUN])
    dispatched: list = []

    async def _execute(*, calls, req, executor_value, deny_tools=None):
        dispatched.extend(calls or [])
        return [], [], {}

    monkeypatch.setattr(cli_tracking, "execute_envelope_tool_calls", _execute)

    result = await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert [c["tool"] for c in dispatched] == ["complete_run"]
    assert result.parse_status == "ok"


@pytest.mark.asyncio
async def test_the_raw_artifact_keeps_the_wire_format(monkeypatch):
    _patch_adapter(monkeypatch)
    _fake_invocations(monkeypatch, [ENVELOPE_RUN])
    stored: dict = {}

    def _put(**kw):
        stored.update(kw)
        return "artifact://x"

    monkeypatch.setattr(cli_tracking.artifact_store, "put", _put)

    await cli_tracking.OpenCodeAdapter().dispatch(_req())

    assert '"type":"step_finish"' in stored["content"]


@pytest.mark.asyncio
async def test_tool_events_reach_the_run_transcript(monkeypatch):
    """opencode runs previously showed a wall of raw text; the tool cards the
    UI already renders for claude_code now work for opencode too."""
    _patch_adapter(monkeypatch)
    _fake_invocations(monkeypatch, [ENVELOPE_RUN])
    events: list[tuple] = []
    monkeypatch.setattr(
        cli_tracking, "append_event", lambda uid, etype, **kw: events.append((etype, kw))
    )

    # The adapter streams through on_chunk, which the fake invoke never calls,
    # so drive the pump the way llm_executor's _pump would.
    reducer = OpenCodeEventReducer()
    emitted = reducer.feed_total(TOOL_RUN)
    for event in emitted:
        cli_tracking._append_stream_event("r-oc", event)

    kinds = [e[0] for e in events]
    assert "tool_use" in kinds and "tool_result" in kinds
    tool_use = next(kw for etype, kw in events if etype == "tool_use")
    # Truncation keeps the input JSON-parseable so the UI can render diffs.
    assert json.loads(tool_use["input"])["filePath"] == "/tmp/a.txt"


# ── codex is unchanged ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_codex_continuation_still_pastes_the_transcript_tail(monkeypatch):
    """codex has no session handle. Its continuation must keep working exactly
    as it did — the opencode change is not allowed to cost codex its second
    pass."""
    _patch_adapter(monkeypatch, provider_kind="codex_subscription")
    calls: list[dict] = []

    async def _invoke(provider, **kw):
        calls.append(kw)
        raw = (
            'I read half the repo.\n'
            if len(calls) == 1
            else '{"tool_calls": [{"tool": "complete_run", "args": {"summary": "s"}}]}'
        )
        return LLMInvocation(raw_output=raw, exit_code=0, transport="cli")

    monkeypatch.setattr(cli_tracking, "invoke_provider", _invoke)

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert len(calls) == 2
    assert calls[1]["cli_session_id"] == ""
    assert "I read half the repo." in calls[1]["instruction"]
    assert "no session resume" in calls[1]["instruction"]
    assert result.usage["continuation_pass"] is True
    # Nothing opencode-shaped attaches itself to a codex run.
    assert "cli_usage" not in result.usage


def test_the_codex_delta_feeder_is_untouched():
    feed = cli_tracking._codex_delta_feeder()
    line = '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
    assert feed(line) == ["hi"]
    assert feed(line + '{"type":"thread.started"}\n') == []
