"""Finding node — faceted output of a Run.

Bugs, security issues, gaps, refactors, proposals, and parse-fallback
observations all share this primitive. They differ only in their facets
(tags / kind / severity / size / subtype). `affected_paths` is the code
anchor; doc pages relate to findings at read time via watch_paths overlap.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    FloatProperty,
    JSONProperty,
    StringProperty,
)


class Finding(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # Facets
    tags = JSONProperty(default=[])
    # Optional free-text labels for filtering ("security", "docs",
    # "structure", "flaky-test", …). Never a required taxonomy.
    kind = StringProperty(required=True, index=True)
    # defect | improvement | gap | proposal | observation | feature-idea
    severity = StringProperty(default="medium", index=True)
    # low | medium | high | critical
    size = StringProperty(default="medium")
    # trivial | small | medium | large — fix-size estimate, NOT the run
    # effort tier (short/normal/deep/unlimited)
    subtype = StringProperty(default="", index=True)
    # free-form, executor-extracted: "god-file", "missing-timeout",
    # "add-node", "split-node", "unparsed-executor-output", etc.

    title = StringProperty(required=True)
    confidence = FloatProperty(default=0.7)

    description = StringProperty(default="")
    # Detailed markdown analysis: what is wrong, where, and how it manifests.
    root_cause = StringProperty(default="")
    # Markdown: why the problem exists (the underlying mechanism, not the symptom).
    why_it_matters = StringProperty(default="")
    evidence = JSONProperty(default={})
    suggested_fix = StringProperty(default="")
    affected_paths = JSONProperty(default=[])

    dedupe_key = StringProperty(unique_index=True, required=True)

    # Provenance
    source_run_uid = StringProperty(index=True)
    source_run_uids = JSONProperty(default=[])
    # Every run that filed OR re-confirmed this finding; lazily backfilled
    # from source_run_uid on the first confirmation (JSON string in Neo4j).
    last_confirmed_at = DateTimeProperty()
    # Last time any run re-found (confirmed) this finding.
    executor = StringProperty(default="manual", index=True)
    # claude_code | opencode | manual (historical rows may carry removed executors)
    source_path = StringProperty(default="tool-call")
    # tool-call | parsed-blob | raw-derived
    parse_status = StringProperty(default="ok")
    # ok | degraded

    # Static-analysis provenance — set when the agent files this finding after
    # investigating a deterministic analyzer candidate (see execution/services/
    # static_analysis.py). Empty for findings the agent discovered on its own.
    detected_by_tool = StringProperty(default="", index=True)
    # ruff | vulture | deptry | semgrep | knip | "" (agent-discovered)
    detected_by_rule = StringProperty(default="")
    # the analyzer's rule/check id the candidate carried (e.g. "F821",
    # a semgrep check_id) — free-form, for cross-referencing the raw output.

    status = StringProperty(default="open", index=True)
    # open | ticketed | acknowledged | wont-fix | fixed | accepted | superseded
    # | dismissed
    #
    # Anything that is not "open" counts as PROCESSED: the findings board hides
    # it by default and surfaces it behind the "Processed" filter. That is a
    # view over this one field, not a second stored flag.

    ticket_uid = StringProperty(default="")
    # The ticket this finding was promoted into, set alongside status
    # "ticketed" so the detail view can link forward. DISPLAY ONLY — never a
    # filter axis. Deleting that ticket clears this (see
    # findings/services/finding_service.py clear_ticket), INDEPENDENTLY of
    # whether the status reverts, so the link cannot outlive its ticket even
    # when the finding was triaged in between. The authoritative edge is still
    # Ticket.origin_finding_uid; this is denormalised because resolving it the
    # other way means scanning every ticket in the org.

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


FINDING_KINDS = {"defect", "improvement", "gap", "proposal", "observation", "feature-idea"}

FINDING_SEVERITIES = {"low", "medium", "high", "critical"}

FINDING_SIZES = {"trivial", "small", "medium", "large"}

FINDING_STATUSES = {
    "open",
    "ticketed",
    "acknowledged",
    "wont-fix",
    "fixed",
    "accepted",
    "superseded",
    "dismissed",
}

FINDING_SOURCE_PATHS = {"tool-call", "parsed-blob", "raw-derived"}

FINDING_PARSE_STATUSES = {"ok", "degraded"}
