"""Minimal fake `codex app-server --stdio` for tests: JSONL JSON-RPC.
Responds to initialize; on thread/start returns a thread id; on turn/start
returns a turn id then emits agentMessage deltas + turn/completed. Deterministic."""
import json, sys

def send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def main():
    thread_seq = 0
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        msg = json.loads(line)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            send({"id": mid, "result": {"userAgent": "fake/0", "codexHome": params.get("_home", ""),
                                        "platformFamily": "unix", "platformOs": "test"}})
        elif method == "initialized":
            pass  # notification, no reply
        elif method == "thread/start":
            thread_seq += 1
            tid = f"thr_{thread_seq}"
            send({"id": mid, "result": {"thread": {"id": tid}}})
            send({"method": "thread/started", "params": {"thread": {"id": tid}}})
        elif method == "turn/start":
            tid = params["threadId"]
            send({"id": mid, "result": {"turn": {"id": f"turn_{tid}", "status": "inProgress"}}})
            text = params["input"][0]["text"]
            if text.startswith("HANG"):
                # Accepted, but never completed — models a turn in flight when
                # the server dies (see the EOF test).
                continue
            reply = f"echo:{text}"
            for ch in (reply[:3], reply[3:]):  # two deltas
                send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "delta": ch}})
            send({"method": "turn/completed", "params": {"threadId": tid, "usage": {"input_tokens": 1}}})
        elif method == "boom/error":
            send({"id": mid, "error": {"code": -32000, "message": "boom"}})
        elif method == "hang/request":
            # Accepted, but never replied — models a REQUEST stuck in _pending
            # (as opposed to "HANG" turn text above, which models a stuck turn).
            continue
        elif method == "stderr/spew":
            # A handful of recognizable stderr lines — enough for a test to
            # assert they were actually drained (and logged) rather than left
            # to accumulate. (A real reproduction of the pipe-fill deadlock
            # needs 10s of MB given asyncio's own internal buffering ahead of
            # any explicit reader, which makes it slow and machine-dependent —
            # asserting the drain happens is the reliable regression signal.)
            for i in range(3):
                print(f"spew-line-{i}", file=sys.stderr)
            sys.stderr.flush()
            send({"id": mid, "result": {}})
        elif method == "huge/line":
            # A single JSON-RPC line bigger than the client's 16MB readline
            # limit — no embedded newline until the very end.
            send({"id": mid, "result": {"payload": "x" * (20 * 1024 * 1024)}})
        elif method == "bad/id":
            # Malformed message: unhashable "id" (a list) — dict.pop(obj["id"])
            # raises TypeError in a naive read loop.
            send({"id": ["bad"], "result": {}})
        else:
            if mid is not None:
                send({"id": mid, "result": {}})

if __name__ == "__main__":
    main()
