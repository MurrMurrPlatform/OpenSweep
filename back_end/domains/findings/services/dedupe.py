"""Stable dedupe-key generation for Findings.

A Finding's dedupe_key collapses repeat reports of the same issue across
multiple audit Runs. The key is derived from:
  repository_uid + normalised title + top affected path

Title normalisation strips numbers, file:line excerpts, and whitespace so
two reports of the same issue produce the same key even if the LLM
rephrased.
"""

import hashlib
import re

_NORMALISE_RE = re.compile(r"[\W_0-9]+")


def _normalise_title(title: str) -> str:
    t = (title or "").lower()
    t = _NORMALISE_RE.sub(" ", t)
    return " ".join(t.split())[:120]


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
            (top_path or "").lower(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
