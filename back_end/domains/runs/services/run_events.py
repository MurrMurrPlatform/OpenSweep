"""Structured per-run transcript events — the stream behind `GET …/transcript`.

PLATFORM_V3_DESIGN.md §4: executors no longer stream raw stdout to the UI.
Every run gets ONE server-parsed event stream, stored append-only at
{ARTIFACT_STORE_ROOT}/runs/{uid}.events.jsonl, one JSON object per line:

    {"seq": int, "ts": "<iso>", "turn": int, "type": "<event type>", ...payload}

Event types:
    user_message   {text}                       every dispatched prompt
    assistant_text {text}                       agent prose (may arrive in chunks;
                                                consecutive events merge in the UI)
    tool_use       {name, input}                input is a truncated preview
    tool_result    {name, output, is_error}     output is a truncated preview
    system         {text, kind}                 markers: sandbox prep, run status…
    turn_end       {status, usage}
    error          {detail}

Deliberately primitive, same contract as the old live log it replaces:
open-append-close per write so concurrent appends can't corrupt each other,
and all failures are swallowed — transcript logging is best-effort
observability and must never break a run. Raw stdout is still retained
separately via `raw_artifact_uri` for debugging.

Live fan-out: every append also publishes the event on Redis
(run_events:<uid>) so watchers (the run WebSocket) wake up without polling.
The publish is a doorbell, not a store — subscribers re-read the FILE by
byte offset (read_events_from), so a lost publish only costs the watcher's
fallback-tick latency.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import settings
from redis_config import get_redis_url

# Preview budgets for tool payloads: enough for a useful collapsible card in
# the UI; the full data lives in the raw artifact.
TOOL_PREVIEW_MAX_CHARS = 4_000
# Structured tool_use inputs keep long string fields (Edit old/new strings,
# Write content) intact up to this per-field budget so the UI can render
# real diffs; the total serialized input is capped separately.
TOOL_INPUT_FIELD_MAX_CHARS = 24_000
TOOL_INPUT_TOTAL_MAX_CHARS = 96_000

# In-process next-seq cache. Appends for one run happen from one process at a
# time, so a plain dict is safe; on restart the counter is re-seeded from the
# last line of the file.
_next_seq: dict[str, int] = {}
# File size after OUR last append. Dispatch ownership of a run can move
# between the backend and the Celery worker (quota redispatch, follow-up
# turns) — when the file grew under the other process the cached counter is
# stale and would mint duplicate seqs, so a size mismatch forces a reseed.
_expected_size: dict[str, int] = {}

# Lazy sync Redis client for the fan-out publish from non-loop callers. After a
# failure publishing is paused for a cooldown so an unreachable Redis never turns
# every append into a connect timeout.
_redis: Any = None
_redis_down_until = 0.0
_REDIS_RETRY_SECONDS = 30.0

# Strong refs to in-flight background publish tasks so the loop doesn't GC them
# mid-flight (asyncio only holds a weak ref to a bare create_task result).
_pending_publishes: set[asyncio.Task] = set()


def run_events_channel(run_uid: str) -> str:
    return f"run_events:{run_uid}"


def _publish(run_uid: str, payload: str) -> None:
    """Best-effort doorbell for live watchers. Silent on any failure.

    The hot caller is the executor's stdout pump — an async coroutine on the
    event loop, hit once per streamed line. A synchronous Redis `publish` there
    blocks the whole loop on the network (and up to the 0.5s socket timeout when
    Redis is unreachable), stalling every other run/request in the process. So
    when a running loop is present we fan out via an async client on a
    fire-and-forget task; only genuinely synchronous callers (no running loop —
    e.g. a Celery task body) take the blocking sync path.
    """
    if time.monotonic() < _redis_down_until:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_publish_async(run_uid, payload))
        _pending_publishes.add(task)
        task.add_done_callback(_pending_publishes.discard)
        return
    _publish_sync(run_uid, payload)


async def _publish_async(run_uid: str, payload: str) -> None:
    """Off-loop publish via the per-loop async Redis client (redis.asyncio)."""
    global _redis_down_until
    try:
        from infrastructure.redis_client import get_async_redis

        await get_async_redis().publish(run_events_channel(run_uid), payload)
    except Exception:  # noqa: BLE001 — fan-out must never break a run
        _redis_down_until = time.monotonic() + _REDIS_RETRY_SECONDS


def _publish_sync(run_uid: str, payload: str) -> None:
    """Blocking publish for synchronous callers (no event loop running)."""
    global _redis, _redis_down_until
    try:
        if _redis is None:
            from redis import Redis

            _redis = Redis.from_url(
                get_redis_url(db=0),
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
        _redis.publish(run_events_channel(run_uid), payload)
    except Exception:  # noqa: BLE001 — fan-out must never break a run
        _redis = None
        _redis_down_until = time.monotonic() + _REDIS_RETRY_SECONDS


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]


def events_path(run_uid: str) -> Path:
    root = getattr(settings, "ARTIFACT_STORE_ROOT", None) or "var/artifacts"
    return Path(root) / "runs" / f"{_safe(run_uid or 'unknown')}.events.jsonl"


def _seed_seq(run_uid: str) -> int:
    """Next seq for a run we haven't appended to this process lifetime."""
    path = events_path(run_uid)
    try:
        with open(path, "rb") as fh:
            last = b""
            for line in fh:
                if line.strip():
                    last = line
        if last:
            return int(json.loads(last).get("seq", 0)) + 1
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return 1


def _write_event_line(
    run_uid: str, type: str, turn: int, payload: dict[str, Any]
) -> tuple[dict[str, Any], str] | None:
    """Seq + serialize + append one event to the file. Silent on any failure.

    Returns (event, serialized_line) so the caller can publish the doorbell and
    derive narration; None when nothing was written. The stat/seq/open/write here
    are the blocking bits — `append_event_async` runs this in a thread so the
    executor pump never touches the disk on the event loop."""
    if not run_uid or not type:
        return None
    try:
        path = events_path(run_uid)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        seq = _next_seq.get(run_uid)
        if seq is None or _expected_size.get(run_uid) != size:
            seq = _seed_seq(run_uid)
        _next_seq[run_uid] = seq + 1
        event = {
            "seq": seq,
            "ts": datetime.now(UTC).isoformat(),
            "turn": turn,
            "type": type,
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
            _expected_size[run_uid] = fh.tell()
    except OSError:
        return None
    return event, line


def _narration_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """The narration sidecar for a tool_use event, or None.

    Narration sidecar (unified dev flow): every tool_use gets a plain-language
    `narration` line in the same stream, carrying covers_seq so the UI can expand
    it into the raw call. Pure templates — never raises; non-tool_use events
    (including narration itself) never narrate."""
    if event.get("type") != "tool_use":
        return None
    try:
        from domains.runs.services.narration import narration_for_event

        return narration_for_event(event)
    except Exception:  # noqa: BLE001 — narration must never break a run
        return None


def append_event(run_uid: str, type: str, *, turn: int = 1, **payload: Any) -> None:
    """Append one transcript event. Silent on any failure. Synchronous — for
    callers not on the event loop (or where a sub-ms local write is fine).
    The executor stdout pump uses `append_event_async` instead."""
    written = _write_event_line(run_uid, type, turn, payload)
    if written is None:
        return
    event, line = written
    _publish(run_uid, line)
    narration = _narration_payload(event)
    if narration is not None:
        append_event(run_uid, "narration", turn=turn, **narration)


async def append_event_async(run_uid: str, type: str, *, turn: int = 1, **payload: Any) -> None:
    """Loop-safe append for the hot path (executor pump, hit per streamed line).

    The blocking stat/open/write runs in a thread so it never stalls the event
    loop; the doorbell publish is off-loop-async via `_publish`. Ordering is
    preserved because the pump awaits each append before reading the next line."""
    written = await asyncio.to_thread(_write_event_line, run_uid, type, turn, payload)
    if written is None:
        return
    event, line = written
    _publish(run_uid, line)
    narration = _narration_payload(event)
    if narration is not None:
        await append_event_async(run_uid, "narration", turn=turn, **narration)


def publish_delta(run_uid: str, text: str, *, turn: int = 1) -> None:
    """Ephemeral token-stream fan-out — published to live watchers, never
    written to the events file. Watchers render these as the in-flight
    message grows; the whole-message assistant_text event appended when the
    block completes is the durable record that replaces them. The payload
    carries no seq — that is how the WS tailer tells it from the appended-
    event doorbells on the same channel."""
    if not run_uid or not text:
        return
    try:
        payload = json.dumps({"type": "delta", "turn": turn, "text": text}, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    _publish(run_uid, payload)


def read_events(run_uid: str, after_seq: int = 0, limit: int = 2_000) -> list[dict[str, Any]]:
    """Events with seq > after_seq, oldest first. [] when nothing exists yet."""
    after_seq = max(0, int(after_seq or 0))
    out: list[dict[str, Any]] = []
    try:
        with open(events_path(run_uid), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and int(event.get("seq", 0)) > after_seq:
                    out.append(event)
                    if len(out) >= limit:
                        break
    except OSError:
        return []
    return out


def read_events_from(
    run_uid: str, offset: int = 0, after_seq: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Incremental read by byte offset — the live-tail path (run WebSocket).

    Returns (events, new_offset). Only complete lines are consumed: a
    trailing partial line stays for the next call, so a read that races an
    in-flight append never yields a truncated event. after_seq filters the
    replay read so a reconnecting client skips events it already has."""
    offset = max(0, int(offset or 0))
    after_seq = max(0, int(after_seq or 0))
    try:
        with open(events_path(run_uid), "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    end = len(data)
    if not data.endswith(b"\n"):
        end = data.rfind(b"\n") + 1  # 0 when no complete line arrived yet
    out: list[dict[str, Any]] = []
    for raw in data[:end].decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and int(event.get("seq", 0)) > after_seq:
            out.append(event)
    return out, offset + end


def preview(value: Any, max_chars: int = TOOL_PREVIEW_MAX_CHARS) -> str:
    """Compact one-blob rendering of a tool input/output for transcript cards."""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            s = str(value)
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


def _truncate_leaves(value: Any, field_max: int) -> Any:
    """Per-leaf string truncation that preserves the container structure."""
    if isinstance(value, str):
        return value if len(value) <= field_max else value[: field_max - 1] + "…"
    if isinstance(value, dict):
        return {str(k): _truncate_leaves(v, field_max) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_leaves(v, field_max) for v in value]
    return value


def preview_structured(
    value: Any,
    field_max: int = TOOL_INPUT_FIELD_MAX_CHARS,
    total_max: int = TOOL_INPUT_TOTAL_MAX_CHARS,
) -> str:
    """JSON rendering of a tool input that stays PARSEABLE after truncation.

    Long string leaves are cut per-field instead of chopping the serialized
    blob mid-token, so the UI can always json-parse the input and render
    file-edit diffs (Edit old/new strings, Write content). Falls back to
    `preview` when the value can't be serialized or the result still exceeds
    the total budget."""
    try:
        s = json.dumps(_truncate_leaves(value, field_max), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return preview(value)
    if len(s) > total_max:
        return preview(value, max_chars=TOOL_PREVIEW_MAX_CHARS)
    return s
