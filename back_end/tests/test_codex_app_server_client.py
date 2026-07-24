import asyncio
import time
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


async def test_start_thread_and_run_turn_streams_and_completes():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        tid = await c.start_thread(cwd="/tmp/x")
        deltas = []
        res = await c.run_turn(thread_id=tid, text="hi", on_delta=deltas.append)
        assert "".join(deltas) == "echo:hi"       # streamed
        assert res.text == "echo:hi" and res.error is None
        assert res.usage.get("input_tokens") == 1
    finally:
        await c.close()


async def test_concurrent_turns_stay_on_their_own_thread():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        ta = await c.start_thread(cwd="/a")
        tb = await c.start_thread(cwd="/b")
        ra, rb = await asyncio.gather(
            c.run_turn(thread_id=ta, text="AAA"),
            c.run_turn(thread_id=tb, text="BBB"),
        )
        assert ra.text == "echo:AAA"
        assert rb.text == "echo:BBB"
    finally:
        await c.close()


# ── Phase 4b activation gates ────────────────────────────────────────────────


async def test_eof_fails_in_flight_turns_instead_of_hanging():
    """A server crash must resolve turns already in flight. Without this, a turn
    with no wall ceiling awaits a completion that can never arrive — forever."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    await c.initialize()
    tid = await c.start_thread(cwd="/x")
    turn = asyncio.create_task(c.run_turn(thread_id=tid, text="HANG"))
    await asyncio.sleep(0.2)          # let the turn start and block
    assert not turn.done()

    await c.close()                   # server dies mid-turn

    res = await asyncio.wait_for(turn, timeout=5)
    assert res.error and "closed" in res.error
    assert not c.alive


async def test_delta_consumer_error_does_not_kill_the_turn():
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        tid = await c.start_thread(cwd="/x")

        def boom(_delta):
            raise RuntimeError("transcript write failed")

        res = await c.run_turn(thread_id=tid, text="hi", on_delta=boom)
        assert res.text == "echo:hi" and res.error is None
    finally:
        await c.close()


async def test_delta_consumer_runs_off_the_shared_read_loop():
    """`on_delta` persists transcript events — real I/O. It must run on the
    turn's OWN task, never inside the client read loop, which is shared by every
    concurrent turn on this app-server."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        tid = await c.start_thread(cwd="/x")
        seen: list = []

        def record(_delta):
            seen.append(asyncio.current_task())

        turn = asyncio.create_task(c.run_turn(thread_id=tid, text="hi", on_delta=record))
        await turn

        assert seen and all(t is turn for t in seen)
        assert all(t is not c._reader for t in seen)
    finally:
        await c.close()


# ── Long-lived, multiplexed-process robustness (code review defects) ────────


async def test_close_fails_in_flight_request_instead_of_hanging():
    """close() must resolve REQUESTS already in flight, not just TURNS.
    `_pending` futures were only ever failed from the EOF branch of the read
    loop — but close() cancels the reader, so once closed nothing was left to
    ever resolve a caller awaiting `request(...)` (e.g. inside `start_thread`)."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    await c.initialize()
    req = asyncio.create_task(c.request("hang/request"))
    await asyncio.sleep(0.2)
    assert not req.done()

    await c.close()

    with pytest.raises(AppServerError) as exc:
        await asyncio.wait_for(req, timeout=5)
    assert "closed" in exc.value.message


async def test_request_does_not_leak_pending_entry_when_write_fails():
    """The future is registered before `await self._write(msg)` on purpose (it
    fixes a response race) — but if the write itself fails, that entry must
    not be left stranded in `_pending` forever."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()

        async def broken_write(msg):
            raise BrokenPipeError("stdin closed")

        c._write = broken_write
        with pytest.raises(BrokenPipeError):
            await c.request("thread/start", {"cwd": "/x"})
        assert c._pending == {}
    finally:
        await c.close()


async def test_stderr_pipe_is_drained_and_logged(monkeypatch):
    """Nothing else ever reads the child's stderr pipe. Left undrained, once
    codex writes enough to fill the kernel pipe buffer the child blocks on
    write(2) forever and every run sharing that session hangs — even though
    `alive` keeps reporting True. Assert the drain actually happens (lines get
    read and logged) rather than trying to reproduce the multi-megabyte
    deadlock directly: asyncio buffers a subprocess pipe internally even with
    no explicit reader, so triggering the real block needs tens of MB and is
    slow/host-dependent — draining-and-logging is the reliable signal that the
    fix (a background reader on `_proc.stderr`) is in place."""
    import domains.llm_providers.services.codex_app_server as mod

    seen: list[str] = []
    monkeypatch.setattr(mod.logger, "debug", lambda msg, *a, **kw: seen.append(msg))

    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        await c.request("stderr/spew")
        await asyncio.sleep(0.2)          # let the background drain task catch up
        assert any("spew-line-0" in s for s in seen)
        assert any("spew-line-2" in s for s in seen)
    finally:
        await c.close()


async def test_read_loop_survives_oversized_line_and_marks_closed():
    """A single line over the 16MB readline limit (e.g. a big tool output
    riding along on item/completed) raises ValueError inside the read loop.
    Uncaught, that kills the reader task silently while `alive` keeps lying."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        with pytest.raises(AppServerError) as exc:
            await asyncio.wait_for(c.request("huge/line"), timeout=5)
        assert "closed" in exc.value.message
        assert not c.alive
    finally:
        await c.close()


async def test_read_loop_survives_malformed_id_and_marks_closed():
    """A message with an unhashable "id" raises TypeError out of
    `_pending.pop(obj["id"], None)`. Uncaught, that kills the reader task
    silently while `alive` keeps lying and the registry keeps handing out a
    broken session."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        with pytest.raises(AppServerError) as exc:
            await asyncio.wait_for(c.request("bad/id"), timeout=5)
        assert "closed" in exc.value.message
        assert not c.alive
    finally:
        await c.close()


async def test_slow_delta_consumer_does_not_drop_other_turns_output():
    """A blocking consumer holds the loop, but the read loop only ever enqueues,
    so the other turn's deltas are buffered rather than lost."""
    c = await AppServerClient.spawn(argv=_FAKE, env={})
    try:
        await c.initialize()
        ta = await c.start_thread(cwd="/a")
        tb = await c.start_thread(cwd="/b")

        def slow(_delta):
            time.sleep(0.25)          # BLOCKING, like a synchronous DB write

        ra, rb = await asyncio.gather(
            c.run_turn(thread_id=ta, text="AAA", on_delta=slow),
            c.run_turn(thread_id=tb, text="BBB"),
        )
        assert ra.text == "echo:AAA" and ra.error is None
        assert rb.text == "echo:BBB" and rb.error is None
    finally:
        await c.close()
