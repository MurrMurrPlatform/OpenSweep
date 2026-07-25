"""Area + AreaEdit nodes — the Area map, OpenSweep's audit partition.

Areas carve the repository into the units audits are planned and tracked
against. Keys are hierarchical slugs ("backend/delivery/convergence");
parent keys are pure groupings derived from prefixes — files belong to
LEAF areas only. Human-owned; agents propose changes as AreaEdits. An area
exists or is deleted — the only workflow state lives on AreaEdit
(pending → accepted | rejected).

The kinds split across TWO independent axes, each a complete partition of
the tree: subsystem areas tile the repository and ignore areas fence off
what audits must skip; feature areas tile it a second time by product flow,
with feature_ignore holding the paths no feature owns (see AREA_KINDS
below). `services/area_coverage.py` is what checks either axis is whole.
"""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)

AREA_KINDS = {
    # ── subsystem axis: every tracked file belongs to exactly one leaf ──
    # subsystem — exclusive partition: every auditable file belongs to
    # exactly one subsystem LEAF.
    "subsystem",
    # ignore — non-auditable files; the spec holds the REASON.
    "ignore",
    # ── feature axis: a second, independent partition of the same tree ──
    # feature — spec-anchored end-to-end flow. It overlays the subsystems,
    # but feature LEAVES tile the tree among themselves.
    "feature",
    # feature_ignore — the feature axis's `ignore`: lockfiles, generated
    # code and static assets that implement no product feature. Without it
    # the feature axis can never be completed, so runs stop half-mapped.
    "feature_ignore",
}

AREA_EDIT_STATUSES = {"pending", "accepted", "rejected"}


class Area(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # Stable identifier, unique per repository (app-enforced, like Doc.slug),
    # path-like: "backend", "backend/delivery/convergence". "/" segments form
    # the hierarchy; parents are groupings, files belong to leaves.
    key = StringProperty(required=True, index=True)
    kind = StringProperty(default="subsystem", index=True)  # AREA_KINDS

    title = StringProperty(default="")
    # Repository path prefixes this area covers. For subsystem/ignore leaves
    # these partition the tree; for features they are references only.
    scope_paths = JSONProperty(default=[])
    # Markdown. feature = the contract the code must honour; subsystem = an
    # optional charter; ignore = the reason these files are not auditable.
    spec = StringProperty(default="")
    # Doc pages that brief this area (uids into the docs domain).
    doc_uids = JSONProperty(default=[])

    enabled = BooleanProperty(default=True, index=True)
    provenance = StringProperty(default="system")  # system | agent | human

    # Last push that touched scope_paths. Stale = code_changed_at newer than
    # last_reviewed_at (derived, never stored).
    code_changed_at = DateTimeProperty()
    # Advances on human edit or accepted AreaEdit.
    last_reviewed_at = DateTimeProperty()
    # Changed paths accumulated since the last review; cleared on review.
    stale_paths = JSONProperty(default=[])

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


class AreaEdit(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # "" = proposes a NEW area (key/kind/title/scope_paths describe it).
    area_uid = StringProperty(default="", index=True)
    key = StringProperty(default="")
    kind = StringProperty(default="")
    title = StringProperty(default="")
    scope_paths = JSONProperty(default=[])
    doc_uids = JSONProperty(default=[])

    # Full replacement markdown; the UI renders the diff against the current spec.
    proposed_spec = StringProperty(default="")
    rationale = StringProperty(default="")

    # Proposed enabled state — False = the agent proposes RETIRING the area
    # (its only way to clean up a bad partition; applied on human accept).
    proposed_enabled = BooleanProperty(default=True)

    # Partition warnings computed at propose time (overlap/leaf conflicts this
    # edit would create). Persisted so the review queue can show them BEFORE a
    # human accepts — never a blocker, advisory only.
    warnings = JSONProperty(default=[])

    source_run_uid = StringProperty(default="", index=True)

    status = StringProperty(default="pending", index=True)  # AREA_EDIT_STATUSES
    resolved_by = StringProperty(default="")
    resolved_at = DateTimeProperty()

    created_at = DateTimeProperty(default_now=True)


def area_is_stale(a: Area) -> bool:
    """Derived: code changed under scope_paths since the area was last
    reviewed. Never stored."""
    if a.code_changed_at is None:
        return False
    if a.last_reviewed_at is None:
        return True
    return a.code_changed_at > a.last_reviewed_at


def child_key_prefix_of(parent_key: str, other_key: str) -> bool:
    """True iff other_key sits strictly under parent_key in the hierarchy.

    THE shared helper for key ancestry — a bare startswith would make
    "backend-jobs" a child of "backend"; the "/" boundary prevents that.
    """
    return other_key.startswith(parent_key + "/")


def is_leaf(key: str, all_enabled_keys: list[str]) -> bool:
    """A key is a leaf when no enabled key sits under it. Files belong to
    leaves; non-leaf keys are pure groupings."""
    return not any(child_key_prefix_of(key, k) for k in all_enabled_keys)
