"""Campaign — the discovery-side Thread: one whole-repo audit effort.

A Campaign partitions a repository into bounded area runs (docs' watch
paths sized against the real file tree) plus global sweeps for the
cross-cutting lenses, dispatches them a few at a time, tracks coverage
per part, and finalizes into one digest. It orchestrates and references
Runs; it never replaces them. Status only ever moves through the service
so every move is legality-checked and audited (mirrors threads/models.py).
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


class Campaign(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    title = StringProperty(default="")

    # planning | running | finalizing | done | failed | cancelled
    status = StringProperty(default="planning", index=True)

    # subsystem | feature | global | batch — the campaign shape/kind.
    kind = StringProperty(default="subsystem", index=True)

    # Which files/areas to select: all | stale | rotation.
    selection = StringProperty(default="all")

    # The area keys this campaign covers (empty = whole map).
    coverage_keys = JSONProperty(default=[])

    # Batch wiring: parent campaign uid (empty for non-batch).
    parent_uid = StringProperty(default="")

    # Batch wiring: child campaign uids (empty for non-batch).
    child_uids = JSONProperty(default=[])

    # full | rotation | focused — how the plan was built (planner.build_plan).
    template = StringProperty(default="rotation")

    # Children's effort tier. "" = default: areas run "normal", global
    # sweeps run "deep" — an explicit tier applies to both.
    effort = StringProperty(default="normal")

    # The lens keys the plan was built from (empty = all enabled).
    lens_keys = JSONProperty(default=[])

    # Rotation only: how many areas this pass covers. Stored so launch can
    # replan with exactly the inputs the campaign was created with.
    k = IntegerProperty(default=3)

    # Area-map campaigns only: restrict the plan to areas at or under this
    # key ("backend/delivery"). "" = the whole map. Stored so launch's
    # replan rebuilds the same slice.
    area_prefix = StringProperty(default="")

    # The plan: [{idx, kind: area|feature|global, title, scope_paths,
    # doc_uids, lens_keys, run_uid, state: pending|running|done|failed,
    # file_count, area_keys}]. area_keys ties map-derived parts back to
    # their Areas — one key per bundled leaf ([] = docs-derived/remainder/
    # global). Part state only moves forward (done/failed are sticky —
    # tick.plan_tick).
    parts = JSONProperty(default=[])

    # How the planner reconciled the area map into `parts` — {source,
    # map_areas, leaves, groupings, features, ignored, area_parts,
    # bundled_leaves, feature_parts, global_parts, oversized, degraded,
    # area_prefix} (campaign_service._plan_parts). Set at create and again
    # by a launch-time replan that changed the parts.
    plan_summary = JSONProperty(default={})

    # How many parts may be in flight at once.
    max_parallel = IntegerProperty(default=2)

    created_by = StringProperty(default="")
    # "manual" | "cron:<expr>" — scheduled campaigns dispatch their children
    # as RunTrigger.SCHEDULE.
    trigger_provenance = StringProperty(default="")

    # Finalize digest: {counts, coverage, failed_parts} (finalize.build_summary).
    summary = JSONProperty(default={})

    # Timeline events: [{ts, type, ...payload}] — planned, launched,
    # part_dispatched, part_done, part_failed, cancelled, finalized.
    events = JSONProperty(default=[])

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


CAMPAIGN_STATUSES = {"planning", "running", "finalizing", "done", "failed", "cancelled"}

CAMPAIGN_TEMPLATES = {"full", "rotation", "focused"}

CAMPAIGN_KINDS = {"subsystem", "feature", "global", "batch"}

CAMPAIGN_SELECTIONS = {"all", "stale", "rotation"}

PART_STATES = {"pending", "running", "done", "failed"}

LEGAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"running", "cancelled"}),
    "running": frozenset({"finalizing", "failed", "cancelled"}),
    "finalizing": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def is_legal_status_transition(frm: str, to: str) -> bool:
    return to in LEGAL_STATUS_TRANSITIONS.get(frm, frozenset())
