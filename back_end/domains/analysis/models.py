"""Analysis node — the rich, interactive output of a deep-scan Run.

One Analysis per Run (unique on source_run_uid). It holds the verdict layer
(health grade + scorecard), the narrative report (an extensible `sections`
dict), auditable lists (coverage/strengths/validation_baseline), and the
interactive `questions` list. Findings are joined at read time via
Finding.source_run_uid — never duplicated here.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


class Analysis(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)
    # Exactly one Analysis per deep-scan Run — the join key for its Findings.
    source_run_uid = StringProperty(unique_index=True, required=True)
    # Commit sha the scan inspected (best-effort; empty when unknown).
    revision = StringProperty(default="")

    title = StringProperty(default="")
    status = StringProperty(default="in_progress", index=True)
    # in_progress | complete | superseded | archived

    # Refine-with-answers chain: a superseding Analysis points back at the one
    # it replaced, and the replaced one is flipped to status=superseded.
    supersedes = StringProperty(default="", index=True)
    superseded_by = StringProperty(default="", index=True)

    executor = StringProperty(default="")

    # ── Verdict layer ────────────────────────────────────────────────────────
    health_grade = StringProperty(default="")  # A | B | C | D | F | ""
    health_score = IntegerProperty()  # optional 0-100, nullable
    # [{dimension, score, max, grade, rationale}] — per-dimension rubric.
    scorecard = JSONProperty(default=[])
    confidence = StringProperty(default="")  # confirmed | high | medium | low | ""
    limitations = StringProperty(default="")  # markdown
    # Free-form computed/reported counts: findings_by_severity, files_scanned…
    stats = JSONProperty(default={})

    # ── Narrative report ─────────────────────────────────────────────────────
    # {section_key -> markdown}. Extensible: new section types need no schema
    # change. Known keys in constants.ANALYSIS_SECTION_KEYS.
    sections = JSONProperty(default={})

    # ── Auditable lists (appended incrementally as the agent works) ───────────
    # [{area, paths[], status: examined|partial|skipped, note}]
    coverage = JSONProperty(default=[])
    # [{title, detail, paths[]}] positive observations
    strengths = JSONProperty(default=[])
    # [{check, command, result, details}] baseline table rows
    validation_baseline = JSONProperty(default=[])

    # ── Interactive layer ────────────────────────────────────────────────────
    # [{uid, question, why_it_matters, category, status, answer,
    #   answered_by, answered_at}] — answerable; drives refine-with-answers.
    questions = JSONProperty(default=[])

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)
    completed_at = DateTimeProperty()


ANALYSIS_STATUSES = {"in_progress", "complete", "superseded", "archived"}

QUESTION_STATUSES = {"open", "answered", "dismissed"}

COVERAGE_STATUSES = {"examined", "partial", "skipped"}

# The auditable-list kinds `add_analysis_note` can append to.
NOTE_TYPES = {"coverage", "strength", "validation"}

# Overall grade + per-dimension grades.
HEALTH_GRADES = {"A", "B", "C", "D", "F"}

CONFIDENCE_LABELS = {"confirmed", "high", "medium", "low"}

# The dimensions a scorecard may rate. Advisory (scorecard is agent-authored
# JSON), used to render a stable order and validate tool input.
SCORE_DIMENSIONS = [
    "correctness",
    "security",
    "performance",
    "reliability",
    "data_integrity",
    "maintainability",
    "testing",
    "documentation",
    "architecture",
    "dependencies",
    "observability",
    "dev_experience",
]

# Known narrative section keys (advisory — `sections` is an open dict).
ANALYSIS_SECTION_KEYS = {
    "executive_summary",
    "repository_map",
    "security_summary",
    "performance_summary",
    "data_integrity_summary",
    "dependency_report",
    "test_gap_report",
    "observability_summary",
    "implementation_plan",
    "top_changes",
}
