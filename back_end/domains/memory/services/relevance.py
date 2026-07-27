"""Relevance ranking for memory retrieval — pure, offline, deterministic.

Retrieval used to be `query in f"{title}\n{body}".lower()`: a substring test
with no ranking. Three concrete failures came out of that:

  - **A near-miss returns nothing.** A run searching "redis fixture flake"
    misses the memory titled "Flaky Redis fixture in the worker tests"
    because the words are in a different order, so the agent re-learns a fact
    the platform already knew.
  - **A hit returns everything, unordered.** Searching "test" on a repo with
    forty memories matches most of them and hands back the ten most recently
    written, which is not the same as the ten most relevant.
  - **Common words count as much as rare ones.** "the worker retry budget"
    matched anything containing "the", and nothing knew that "retry" was the
    word that carried the query.

So: tokenise both sides, weight each query term by how rare it is across the
repository's own memories (IDF over the candidate set — no corpus to ship, no
model to load), score title hits above body hits, and use recency only as a
tiebreak. Everything here is a pure function over plain data so it can be
unit-tested without a database, and it deliberately uses nothing outside the
standard library: memory retrieval runs inside run dispatch, and dispatch
must not grow a model download or a network call.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

#: Words split on any non-alphanumeric run, so "src/api/handlers.py",
#: "run_policy" and "retry-budget" all break into their parts.
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

#: …and identifiers split again on camel-case boundaries, because agents
#: write memories about `SandboxService` and search for "sandbox service".
#: The first alternative keeps acronym runs whole ("HTTPClient" → http,
#: client), the rest take ordinary CamelCase and digit runs.
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Dropped before scoring. Two groups: English function words, which appear
#: in nearly every memory and would otherwise let an unrelated memory "match"
#: a query, and file-extension noise from path-shaped queries — a run scoped
#: to `src/api/handlers.py` should not be told that every `.py` memory is
#: relevant. Kept deliberately short: IDF already demotes frequent words, and
#: an over-eager stopword list silently deletes real query terms.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "but", "by", "can",
        "do", "for", "from", "has", "have", "if", "in", "into", "is", "it",
        "its", "must", "no", "not", "of", "on", "or", "should", "so", "than",
        "that", "the", "their", "then", "there", "these", "they", "this",
        "to", "was", "were", "when", "which", "will", "with", "you", "your",
        # File-extension noise.
        "js", "json", "jsx", "md", "py", "sql", "ts", "tsx", "yaml", "yml",
    }
)

#: A term in the title is a term the memory is ABOUT; the same term in the
#: body may be incidental context. Both count, the title counts for more.
_TITLE_WEIGHT = 1.0
_BODY_WEIGHT = 0.55

#: The old substring behaviour was not worthless — a literal phrase hit is a
#: strong signal that token overlap alone cannot express ("run policy" as a
#: phrase vs. the two words scattered). Kept as a bonus on top of the
#: token score rather than as the gate it used to be.
_TITLE_PHRASE_BONUS = 0.20
_BODY_PHRASE_BONUS = 0.10

#: Recency is a TIEBREAK, not a ranking signal: capped low enough that a
#: fresh irrelevant memory can never outrank a relevant older one.
_RECENCY_BONUS = 0.05
_RECENCY_HALF_LIFE_DAYS = 60.0

#: A memory whose anchor Doc's code changed after the memory was written may
#: describe code that no longer exists. It is still shown (it is often still
#: true, and the caller labels it), but it loses to an equally-relevant fresh
#: memory. This replaces the old hard "all fresh before any stale" ordering,
#: which let a stale-but-irrelevant memory outrank the one the run needed.
_STALE_PENALTY = 0.25


def tokenize(text: str) -> list[str]:
    """Lowercased content words of `text`, camel-case and path segments split.

    Order is preserved (callers that want a set can build one); duplicates are
    kept so a caller could weight term frequency, though `relevance_score`
    deliberately does not — memories are a paragraph long, and repeating a
    word three times in one is noise, not emphasis.
    """
    tokens: list[str] = []
    for chunk in _WORD_RE.findall(text or ""):
        for part in _CAMEL_RE.findall(chunk):
            lowered = part.lower()
            if lowered and lowered not in _STOPWORDS:
                tokens.append(lowered)
    return tokens


def idf_weights(documents: Iterable[Sequence[str]]) -> dict[str, float]:
    """Inverse document frequency of every token, over the candidate set.

    The "corpus" is this repository's own memories, computed per query. That
    is the point: rarity is relative to what this repo has written down, so on
    a repo where every memory mentions "celery" the word carries almost no
    weight, while on a repo where one memory does it carries a lot.

    Smoothed so the weight is always positive — an unsmoothed `log(N/df)` is
    zero when a term is in every document, which would make a single-memory
    repository score everything at zero and return nothing.
    """
    doc_count = 0
    frequency: dict[str, int] = {}
    for tokens in documents:
        doc_count += 1
        for token in set(tokens):
            frequency[token] = frequency.get(token, 0) + 1
    return {
        token: math.log(1.0 + (doc_count + 1) / (1 + df))
        for token, df in frequency.items()
    }


def _phrase_bonus(query: str, title: str, body: str) -> float:
    """Literal-substring credit, i.e. what the old implementation tested."""
    phrase = " ".join((query or "").lower().split())
    if len(phrase) < 3:
        return 0.0
    if phrase in (title or "").lower():
        return _TITLE_PHRASE_BONUS
    if phrase in (body or "").lower():
        return _BODY_PHRASE_BONUS
    return 0.0


def relevance_score(
    *,
    query: str,
    title: str,
    body: str,
    weights: dict[str, float] | None = None,
) -> float:
    """How well one memory answers `query`, as a 0..1-ish lexical score.

    The token part is normalised by the total IDF mass of the query, so the
    score reads as "what fraction of the query's *information* this memory
    covers" rather than "how many words happened to collide". Without that
    normalisation a long memory wins every query by accident.
    """
    query_tokens = list(dict.fromkeys(tokenize(query)))
    if not query_tokens:
        return 0.0
    weights = weights or {}
    title_tokens = set(tokenize(title))
    body_tokens = set(tokenize(body))

    total = 0.0
    matched = 0.0
    for token in query_tokens:
        # A query word that appears in no memory at all has no IDF entry; it
        # still counts against coverage (an unanswerable term means the
        # memory does not cover the query) at the weight of a rare word.
        weight = weights.get(token, math.log(2.0))
        total += weight
        if token in title_tokens:
            matched += weight * _TITLE_WEIGHT
        elif token in body_tokens:
            matched += weight * _BODY_WEIGHT
    if total <= 0.0:
        return 0.0
    return matched / total


def _recency_bonus(updated_at: datetime | None, now: datetime) -> float:
    if updated_at is None:
        return 0.0
    stamp = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    age_days = max((now - stamp).total_seconds() / 86400.0, 0.0)
    return _RECENCY_BONUS * math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS)


def _scored_rows(
    *,
    query: str,
    memories: Sequence[Any],
    stale_uids: Iterable[str],
    now: datetime,
) -> list[tuple[float, float, Any]]:
    """(final score, topical score, memory) triples, best first.

    The topical score is kept alongside the final one because they answer
    different questions: the final score orders the results, the topical score
    says whether the memory is about the query at all — and recency and
    staleness must not be allowed to decide that.
    """
    stale = set(stale_uids)
    corpus = [tokenize(f"{m.title or ''}\n{getattr(m, 'body', '') or ''}") for m in memories]
    weights = idf_weights(corpus)

    rows: list[tuple[float, float, Any]] = []
    for m in memories:
        title = m.title or ""
        body = getattr(m, "body", "") or ""
        topical = relevance_score(query=query, title=title, body=body, weights=weights)
        topical += _phrase_bonus(query, title, body)
        score = topical + _recency_bonus(getattr(m, "updated_at", None), now)
        if getattr(m, "uid", "") in stale:
            score -= _STALE_PENALTY
        rows.append((score, topical, m))

    # Sort on score, then recency, then uid. The last two keys make the order
    # total and deterministic: two memories written in the same second used to
    # come back in whatever order the driver yielded them, which made both the
    # briefing text and its tests flaky.
    rows.sort(
        key=lambda row: (
            -row[0],
            -_stamp_seconds(getattr(row[2], "updated_at", None)),
            getattr(row[2], "uid", ""),
        )
    )
    return rows


def score_memories(
    *,
    query: str,
    memories: Sequence[Any],
    stale_uids: Iterable[str] = (),
    now: datetime | None = None,
) -> list[tuple[float, Any]]:
    """Score every memory against `query`, highest first.

    Accepts anything with `.uid`, `.title`, `.body` and `.updated_at` so it
    can be exercised on plain objects in tests, and so it never has to touch
    the database itself.

    An empty query is not an error — it is the repo-wide briefing case, where
    there is no scope to match against. Every memory then scores on recency
    and staleness alone, which reproduces the previous "fresh first, newest
    first" listing.
    """
    rows = _scored_rows(
        query=query,
        memories=memories,
        stale_uids=stale_uids,
        now=now or datetime.now(UTC),
    )
    return [(score, m) for score, _topical, m in rows]


def _stamp_seconds(value: datetime | None) -> float:
    if value is None:
        return 0.0
    stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
    return stamp.timestamp()


def rank_memories(
    *,
    query: str,
    memories: Sequence[Any],
    stale_uids: Iterable[str] = (),
    limit: int = 10,
    now: datetime | None = None,
) -> list[Any]:
    """`score_memories`, filtered to actual matches and capped at `limit`.

    A non-empty query drops zero-scoring memories: the caller is putting these
    in a prompt, and a memory that shares no content word with the run's scope
    is prompt budget spent on noise. An empty query keeps everything, because
    there was nothing to fail to match.
    """
    rows = _scored_rows(
        query=query,
        memories=memories,
        stale_uids=stale_uids,
        now=now or datetime.now(UTC),
    )
    if tokenize(query):
        # Cut on the TOPICAL score: the stale penalty and the recency bonus
        # reorder matches, they must never turn a real match into a non-match
        # (or a non-match into a result).
        rows = [row for row in rows if row[1] > 0.0]
    return [m for _score, _topical, m in rows[: max(1, limit)]]
