"""Checked node — "last investigated" stamps (KNOWLEDGE_V3_CHECKED.md).

One stamp per scope per run. Replaces the CoverageRecord node×concern
matrix: the only question coverage must answer is "has this part of the
repo been looked at since it last changed", and that is derived at query
time from these stamps vs Doc.code_changed_at.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)


class Checked(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # Doc uid, or the repository uid for repo-wide runs.
    scope_uid = StringProperty(required=True, index=True)

    run_uid = StringProperty(required=True, index=True)

    revision = StringProperty(default="")  # commit sha inspected

    # clean | findings | failed — failed also covers cancelled/limit_exceeded:
    # for freshness purposes they all mean "this look did not complete".
    outcome = StringProperty(default="clean", index=True)

    checked_at = DateTimeProperty(default_now=True, index=True)

    # Coverage contract (complete_run → Run.usage["coverage"]): what this look
    # CLAIMS to have examined. Falls back to the run's target paths when the
    # agent didn't report covered_paths. Entries are dropped when they name a
    # path the repository does not have (checked_service._validate_claim).
    covered_paths = JSONProperty(default=[])  # repo-relative paths examined
    skipped_paths = JSONProperty(default=[])  # in-scope paths not examined
    # What the run was OBSERVED to open, derived from its own tool_use events
    # (checked/services/observed_coverage.py). `covered_paths` is a claim;
    # this is a measurement, and the gap between them is the useful number.
    #
    # An empty list is ambiguous by itself: it means "measured, found nothing"
    # only when coverage_source == "observed", and "not measured" otherwise —
    # an opencode run (whose adapter emits no per-tool-call events at all), or
    # a stamp predating this field. The discriminator therefore lives on
    # coverage_source rather than in this list's emptiness, which is also why
    # this field needs no backfill migration.
    covered_paths_observed = JSONProperty(default=[])
    # observed | reported | inferred | unknown — strongest evidence first:
    #   observed  derived from the run's own tool calls.
    #   reported  the agent declared covered_paths and they check out.
    #   inferred  the agent declared nothing, so covered_paths is really the
    #             dispatched target: what the run was POINTED at rather than
    #             what it read. Kept distinct so a stamp can never silently
    #             pass off intent as evidence.
    #   unknown   predates m0022; readers must treat it as no better than
    #             inferred.
    #
    # Defaults to "unknown", not "reported": stamps written before that field
    # existed cannot be classified after the fact, and defaulting them to
    # "reported" would have the whole backlog assert evidence it never had.
    # record_for_run always writes an explicit value (m0022 backfills the rest).
    coverage_source = StringProperty(default="unknown", index=True)
    # [{lens, verdict: checked-clean|checked-findings|skipped, note?}] — one
    # entry per audit lens the run was assigned.
    lens_verdicts = JSONProperty(default=[])


CHECKED_OUTCOMES = {"clean", "findings", "failed"}

#: Ordered strongest-evidence-first — readers that rank or badge coverage
#: honesty should use this rather than re-listing the values.
COVERAGE_SOURCES = ("observed", "reported", "inferred", "unknown")
