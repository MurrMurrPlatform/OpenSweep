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
            reply = f"echo:{text}"
            for ch in (reply[:3], reply[3:]):  # two deltas
                send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "delta": ch}})
            send({"method": "turn/completed", "params": {"threadId": tid, "usage": {"input_tokens": 1}}})
        elif method == "boom/error":
            send({"id": mid, "error": {"code": -32000, "message": "boom"}})
        else:
            if mid is not None:
                send({"id": mid, "result": {}})

if __name__ == "__main__":
    main()
