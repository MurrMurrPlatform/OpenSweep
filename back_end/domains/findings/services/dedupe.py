"""Stable dedupe-key generation for Findings.

A Finding's dedupe_key collapses repeat reports of the same issue across
multiple audit Runs. The key is derived from:
  repository_uid + normalised title + BASENAME of the top affected path

Title normalisation strips numbers, file:line excerpts, and whitespace so
two reports of the same issue produce the same key even if the LLM
rephrased.

The path contributes only its basename because the full path made the key
move with the file: relocating `src/auth/session.py` to
`src/core/session.py` changed the key, so the next audit filed the same issue
again as a new Finding. That silently forked `source_run_uids` — the
cross-run corroboration count, which is the cheapest false-positive signal
the platform has (see `services/trust.py`).

Changing the key does not rewrite existing rows: a finding stored under an
old full-path key keeps it, and the next report computes the new one. The
similarity fallback in `platform_tools/create_finding` closes that gap — it
now matches on basenames too, so the re-report merges into the existing
finding instead of duplicating it.
"""

import difflib
import hashlib
import re

_NORMALISE_RE = re.compile(r"[\W_0-9]+")


def path_basename(path: str) -> str:
    """Last segment of a repo-relative path, lowercased.

    Deliberately not `os.path.basename`: these are always repo-relative
    POSIX-style paths from the executor, and the host's separator is
    irrelevant. Entries may carry the "path:line" convention, so a trailing
    ":<line>" is stripped first.
    """
    text = (path or "").strip().replace("\\", "/")
    if not text:
        return ""
    head, sep, tail = text.rpartition(":")
    if sep and tail.isdigit():
        text = head
    return text.rstrip("/").rpartition("/")[2].lower()


def _normalise_title(title: str) -> str:
    t = (title or "").lower()
    t = _NORMALISE_RE.sub(" ", t)
    return " ".join(t.split())[:120]


def titles_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """True when two finding titles read as the same issue after
    normalisation (case/numbers/punctuation stripped). Used by the
    create_finding similarity fallback when the exact dedupe_key misses —
    e.g. an LLM rephrasing "SQL injection in the /users endpoint" as
    "Possible SQL injection in the users endpoint"."""
    na = _normalise_title(a)
    nb = _normalise_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def build_dedupe_key(
    *,
    repository_uid: str,
    title: str,
    top_path: str,
) -> str:
    raw = "|".join(
        [
            (repository_uid or ""),
            _normalise_title(title),
            path_basename(top_path),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
