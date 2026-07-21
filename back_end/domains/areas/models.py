"""Area + AreaEdit nodes — the Area map, OpenSweep's audit partition.

Areas carve the repository into the units audits are planned and tracked
against. Keys are hierarchical slugs ("backend/delivery/convergence");
parent keys are pure groupings derived from prefixes — files belong to
LEAF areas only. Human-owned; agents propose changes as AreaEdits. An area
exists or is deleted — the only workflow state lives on AreaEdit
(pending → accepted | rejected).

Three kinds partition the map's semantics: subsystem areas tile the
repository, feature areas overlay it, ignore areas fence off what audits
must skip (see AREA_KINDS below).
"""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)

AREA_KINDS = {
    # subsystem — exclusive partition: every auditable file belongs to
    # exactly one subsystem LEAF.
    "subsystem",
    # feature — spec-anchored cross-cutting overlay: references paths,
    # no exclusivity.
    "feature",
    # ignore — non-auditable files; the spec holds the REASON.
    "ignore",
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
