"""What a run ACTUALLY opened, derived from its own tool calls.

Coverage was a self-report with a fallback that relabelled the dispatch order
as a result: when an agent declared no `covered_paths`, `record_for_run` wrote
the run's target paths instead and marked the stamp `inferred`. So a run that
read one file of a 35-page scope stamped the whole scope as covered, and every
downstream reader — the areas board, the coverage strip, campaign rotation —
consumed that as if it were evidence.

The evidence was already on disk. Every run writes a structured, durable event
stream (`run_events`, `{ARTIFACT_STORE_ROOT}/runs/{uid}.events.jsonl`) whose
`tool_use` events carry `{name, input}`, and `preview_structured` guarantees
the input stays JSON-parseable — a `Read`'s `file_path` is tens of characters,
nowhere near the 24k per-field truncation budget. This module turns that into
a path list.

**Only content-bearing tools count.** Read/NotebookRead/Edit/Write actually put
a file's text in front of the model. Grep and Glob are deliberately EXCLUDED:
a `Grep` over `back_end/` would otherwise stamp the whole tree as examined,
which is the same overclaim this module exists to end. Searching a directory
is how you decide what to read, not evidence that you read it.

**claude_code only.** The opencode adapter has no per-tool-call events at all
(`llm_executor` pumps raw text and `cli_tracking` turns it straight into
`assistant_text`), so an opencode run yields nothing here. That is a silent
absence of evidence, not evidence of absence — `Checked.coverage_source`
stays `reported`/`inferred` for those runs so no reader mistakes one for the
other.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from domains.repositories.services.path_matching import normalize_path

#: Tool names whose input names a file the model actually saw the contents of.
#: Lowercased for comparison; the harness spells them `Read`, `NotebookEdit`, …
_CONTENT_TOOLS = {
    "read",
    "notebookread",
    "notebookedit",
    "edit",
    "multiedit",
    "write",
}

#: Input keys that carry a path, in priority order (mirrors
#: `runs.services.narration.narrate_tool_use`, which reads the same events).
_PATH_KEYS = ("file_path", "notebook_path", "path")


def _parse_input(raw: Any) -> dict[str, Any]:
    """`tool_use.input` as a dict — it is serialized JSON on the wire.

    `preview_structured` falls back to a non-parseable preview when the whole
    input exceeds its total budget (a huge Write body). That costs us one
    path, never a crash.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _relative_to_workspace(path: str, workspace_root: str) -> str:
    """A repo-relative path, or "" when it falls outside the workspace.

    The harness reports absolute paths (`/workspace/<uid>/back_end/x.py`).
    Anything outside the clone — `/etc/hosts`, a temp file, the agent's own
    scratch — is not repository coverage and is dropped rather than recorded
    under a mangled name.
    """
    text = (path or "").strip().replace("\\", "/")
    if not text:
        return ""
    root = (workspace_root or "").strip().replace("\\", "/").rstrip("/")
    if text.startswith("/"):
        if not root or not (text == root or text.startswith(root + "/")):
            return ""
        text = text[len(root) :]
    return normalize_path(text.lstrip("/"))


def paths_from_tool_use(name: str, raw_input: Any, workspace_root: str) -> list[str]:
    """The repo-relative paths one `tool_use` event evidences. Pure."""
    if (name or "").strip().lower() not in _CONTENT_TOOLS:
        return []
    data = _parse_input(raw_input)
    out: list[str] = []
    for key in _PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            rel = _relative_to_workspace(value, workspace_root)
            if rel:
                out.append(rel)
    return out


def observed_paths(events: Iterable[dict], *, workspace_root: str) -> list[str]:
    """Repo-relative paths a run opened, de-duplicated, first-seen order. Pure.

    Takes any iterable of event dicts so callers can stream a large events
    file instead of materializing it.
    """
    seen: dict[str, None] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            continue
        for path in paths_from_tool_use(
            str(event.get("name") or ""), event.get("input"), workspace_root
        ):
            seen.setdefault(path, None)
    return list(seen)


def _stream_events(run_uid: str) -> Iterator[dict]:
    """Every event in a run's JSONL, streamed.

    Deliberately not `run_events.read_events`: that caps at 2000 events, and a
    deep audit run blows through that long before it finishes reading files —
    a cap here would silently under-report coverage, which is the failure mode
    this module exists to remove.
    """
    from domains.runs.services.run_events import events_path

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
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def observed_paths_for_run(run_uid: str, workspace_root: str) -> list[str]:
    """`observed_paths` over a run's on-disk event stream.

    Returns [] when the stream is missing, unreadable, or carries no
    content-tool calls (every opencode run, and any claude run that answered
    from the briefing alone).
    """
    if not run_uid:
        return []
    return observed_paths(_stream_events(run_uid), workspace_root=workspace_root)
