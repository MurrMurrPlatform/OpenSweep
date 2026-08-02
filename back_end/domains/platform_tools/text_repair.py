"""Repair tool arguments that arrived JSON-escaped instead of decoded.

Agents that relay a document through a subagent report — or hand-write an
argument from an escaped transcript — sometimes send the *escaped* form of a
markdown body: one physical line where every newline is a literal ``\\n`` and
every markdown special is backslash-escaped (``\\# Backend`` instead of
``# Backend``). Stored verbatim, the page renders as a single unreadable line
of source. The model, not OpenSweep, produced the escaping, so no prompt
change makes it impossible — the ingestion boundary decodes it instead.

Deliberately conservative. Only a long, single-physical-line string carrying
several ``\\n`` markers is touched; anything with a real newline is already
well-formed and is returned untouched. Escapes of *letters* (``\\d``, ``\\w``)
are preserved so regexes and Windows paths in evidence text survive.
"""

from __future__ import annotations

import json
from typing import Any

# A body short enough to fit on one line, or with only a marker or two, is far
# more likely to be prose *about* "\n" than a mangled document.
_MIN_LENGTH = 200
_MIN_MARKERS = 3

_SIMPLE = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "/": "/",
}

# Markdown escapes the model added on its way out (``\#``, ``\*``, ``\|``).
# Punctuation only: `\d`/`\s` in a regex must keep its backslash.
_PUNCTUATION = set("#*_~`[](){}<>+-.!|$&^=%@,;:?")


def _is_json_payload(text: str) -> bool:
    """A serialized JSON object/array — an agent attaching a log or a trace as
    `content`. Its `\\n`s are correct encoding, not damage."""
    if text[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def looks_escaped(text: str) -> bool:
    """True when `text` is one long line of backslash-escaped source."""
    if len(text) < _MIN_LENGTH or "\n" in text or "\r" in text:
        return False
    markers = sum(text.count(f"\\{c}") for c in "nrt")
    return markers >= _MIN_MARKERS and not _is_json_payload(text)


def repair_text(value: str) -> str:
    """Decode a string that was escaped as if for JSON; pass anything else
    through untouched."""
    if not isinstance(value, str) or not looks_escaped(value):
        return value
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt in _SIMPLE:
            out.append(_SIMPLE[nxt])
            i += 2
        elif nxt == "u" and i + 6 <= n:
            try:
                out.append(chr(int(value[i + 2 : i + 6], 16)))
                i += 6
            except ValueError:
                out.append(ch)
                i += 1
        elif nxt in _PUNCTUATION:
            out.append(nxt)
            i += 2
        else:
            # `\d`, `\w`, `\P` — meaningful as written, keep both characters.
            out.append(ch)
            i += 1
    return "".join(out)


def repair_args(value: Any) -> Any:
    """Apply `repair_text` to every string inside a tool-argument structure."""
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, dict):
        return {k: repair_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [repair_args(v) for v in value]
    return value
