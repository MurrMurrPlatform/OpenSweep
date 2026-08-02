"""P3 — run-event fan-out stays off the event loop.

The executor's stdout pump calls into run_events once per streamed line while
running as a coroutine on the event loop. The Redis publish (network I/O, up to
a 0.5s socket timeout when Redis is down) and the transcript file write are the
blocking bits. This asserts the hot path (append_event_async / publish_delta)
routes the publish to the off-loop async client and the disk write to a thread,
never taking the blocking sync path while a loop is running — and that a purely
synchronous caller still uses the blocking path.
"""

import asyncio
import json

import pytest

from domains.runs.services import run_events


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(run_events.settings, "ARTIFACT_STORE_ROOT", str(tmp_path))
    run_events._next_seq.clear()
    run_events._expected_size.clear()
    run_events._redis_down_until = 0.0
    yield


async def _drain_publishes():
    for _ in range(5):
        pending = list(run_events._pending_publishes)
        if not pending:
            break
        await asyncio.gather(*pending)


async def test_hot_path_publishes_async_never_sync(monkeypatch):
    def _sync_boom(*_a, **_k):
        raise AssertionError("blocking sync publish on the event loop")

    published: list[tuple[str, str]] = []

    async def _fake_async(run_uid, payload):
        published.append((run_uid, payload))

    monkeypatch.setattr(run_events, "_publish_sync", _sync_boom)
    monkeypatch.setattr(run_events, "_publish_async", _fake_async)

    await run_events.append_event_async("run-a", "system", text="hello")
    run_events.publish_delta("run-a", "tok")  # sync fn, on the loop → async fan-out
    await _drain_publishes()

    # The event landed in the file (write happened, off the loop via a thread).
    events = run_events.read_events("run-a")
    assert [e["type"] for e in events] == ["system"]

    # Both doorbells went out via the async client; none via the blocking path.
    assert len(published) == 2
    append_payload = json.loads(published[0][1])
    assert append_payload["type"] == "system" and append_payload["seq"] == 1
    delta_payload = json.loads(published[1][1])
    assert delta_payload["type"] == "delta" and delta_payload["text"] == "tok"


async def test_tool_use_narration_stays_off_loop(monkeypatch):
    def _sync_boom(*_a, **_k):
        raise AssertionError("blocking sync publish on the event loop")

    monkeypatch.setattr(run_events, "_publish_sync", _sync_boom)

    async def _fake_async(_run_uid, _payload):
        return None

    monkeypatch.setattr(run_events, "_publish_async", _fake_async)

    # A tool_use event emits a narration sidecar — both must go through the
    # async path without touching the disk on the loop.
    await run_events.append_event_async(
        "run-n", "tool_use", name="Read", input={"file_path": "/x"}
    )
    await _drain_publishes()

    types = [e["type"] for e in run_events.read_events("run-n")]
    assert "tool_use" in types
    assert "narration" in types  # sidecar written via the async recursion


def test_sync_caller_uses_blocking_publish(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(run_events, "_publish_sync", lambda u, p: calls.append((u, p)))

    def _async_boom(*_a, **_k):
        raise AssertionError("async path used off the loop")

    monkeypatch.setattr(run_events, "_publish_async", _async_boom)

    # No running loop here → the synchronous append takes the blocking publish.
    run_events.append_event("run-b", "system", text="x")

    assert len(calls) == 1 and calls[0][0] == "run-b"
    assert run_events.read_events("run-b")[0]["type"] == "system"
