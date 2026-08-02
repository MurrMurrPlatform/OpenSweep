"""The review axis, in one place.

Docs and Areas answer the same question — "has anyone looked since the code
moved?" — and answered it with four separate copies of the same rule:
`docs.models.doc_is_stale`, `areas.models.area_is_stale`, an inline
re-expression in `runs.services.audit_selection.rank_targets`, and a Cypher
mirror in `metrics.services.attention_service`. They agreed, but nothing made
them agree. Everything derives through here now, and the Cypher one
interpolates `STALE_WHERE` so a change to the rule breaks both or neither.

Two halves of one axis:

- READ  — `is_stale(code_changed_at, last_reviewed_at)`. Derived on every read,
  NEVER stored: a persisted flag would be a second source of truth that drifts
  from the webhook.
- WRITE — `mark_reviewed(node, now)`. The review side (human edit, accepted
  edit, `confirm_*_current`) advances `last_reviewed_at` and clears the
  accumulated `stale_paths`. The staleness side lives in
  `repositories.services.path_matching`, which stamps `code_changed_at`.

Deliberately takes two datetimes rather than a node, so `PageInfo` dataclasses
and neomodel nodes share one implementation. Its only `domains` dependency is
`path_matching.normalize_path`, itself a leaf — so models import this without
a cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class _Reviewable(Protocol):
    """Anything carrying the review axis — Doc, Area, or a projection."""

    code_changed_at: datetime | None
    last_reviewed_at: datetime | None


def is_stale(
    code_changed_at: datetime | None, last_reviewed_at: datetime | None
) -> bool:
    """Has code moved under this node's watched paths since it was reviewed?

    Note the asymmetric null handling, which is the whole semantics:
    never-changed is NOT stale (a page nothing has touched needs no review),
    but changed-and-never-reviewed IS. A node with no watched paths can never
    have `code_changed_at` stamped at all, so it is permanently not-stale —
    see `is_tracked`, which is how a surface tells that apart from "fresh".
    """
    if code_changed_at is None:
        return False
    if last_reviewed_at is None:
        return True
    return code_changed_at > last_reviewed_at


def node_is_stale(node: _Reviewable) -> bool:
    """`is_stale` for anything with the two timestamps as attributes."""
    return is_stale(
        getattr(node, "code_changed_at", None), getattr(node, "last_reviewed_at", None)
    )


def is_tracked(watched_paths: Any) -> bool:
    """Does this node have a path anchor the webhook can actually match?

    Staleness is only ever stamped by matching a changed path against
    `watch_paths`/`scope_paths`, so a node with none is invisible to the
    webhook — permanently, silently not-stale. That is NOT the same as fresh,
    and a surface that renders it green is lying.

    Normalizes rather than testing truthiness: `mark_nodes_stale` drops paths
    that normalize away, so `["/"]` or `[" "]` is just as unmatchable as `[]`
    and must report the same. Agent-proposed scopes are not normalized at the
    write side, so this is reachable.
    """
    from domains.repositories.services.path_matching import normalize_path

    return any(normalize_path(str(p)) for p in (watched_paths or []))


def mark_reviewed(node: Any, now: datetime) -> None:
    """Advance the review stamp and drop the accumulated changed paths.

    `stale_paths` is the "what changed since you last looked" briefing list, so
    it is meaningless the moment the node is reviewed.
    """
    node.last_reviewed_at = now
    node.stale_paths = []


def review_snapshot(node: Any, fields: tuple[str, ...]) -> tuple:
    """A comparable image of the fields whose change constitutes a review.

    Compared by VALUE, not by "was this field present in the request": the
    edit dialogs send every field on every save, so a presence check would
    call each one an edit.

    Normalizes so a cosmetic write is not mistaken for a re-check:
      - strings compare stripped — a trailing newline from an editor is not a
        claim that the prose was re-read against the code;
      - path lists compare as a SET of normalized entries — order and
        duplicates carry no meaning for a set of path prefixes, and a reorder
        would otherwise clear a stale badge.

    Copies list values so `before` cannot alias a field the caller then
    mutates in place.
    """
    return tuple(_comparable(getattr(node, f, None)) for f in fields)


def _comparable(value: Any):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return frozenset(str(v).strip() for v in value if str(v).strip())
    return value


# The Cypher half of `is_stale`, for the aggregate counts that cannot round-trip
# through Python. Interpolate rather than re-typing the rule:
#     f"MATCH (a:Area {{...}}) WHERE {STALE_WHERE.format(n='a')} RETURN count(a)"
STALE_WHERE = (
    "{n}.code_changed_at IS NOT NULL "
    "AND ({n}.last_reviewed_at IS NULL OR {n}.code_changed_at > {n}.last_reviewed_at)"
)
