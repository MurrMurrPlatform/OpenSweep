"""Aggregate queries for the overview dashboard.

PLATFORM.md model: counts derive from Findings (faceted), Doc pages, and
Runs.
"""

import json
from datetime import UTC, datetime, timedelta

from neomodel import adb

from domains.metrics.schemas import (
    FindingStatusCount,
    FindingTagCount,
    OverviewMetrics,
    RepoSummary,
)

# Statuses that still represent REAL, OUTSTANDING work. This is not the same
# question the findings board asks: the board hides anything not "open" so the
# triage inbox stays actionable, but a problem does not stop existing because
# someone acknowledged it or opened a ticket for it. `ticketed` belongs here
# for the same reason `acknowledged` always has — dropping it would have made
# the headline count fall by every historically-promoted finding the moment
# m0020 backfilled them.
_OPEN_FINDING_STATUSES = {"open", "acknowledged", "ticketed"}
_HIGH_SEVERITY = {"high", "critical"}

# Kinds that count as "real work" in the open-findings tally. Proposals are
# excluded — they show up in their own dashboard counter (`proposals`) and
# in the Proposals inbox view; counting them as "open issues" double-counts
# and confuses the headline number.
_REAL_FINDING_KINDS = {"defect", "improvement", "gap", "observation"}


class MetricsService:
    async def overview(self, org_uid: str) -> OverviewMetrics:
        # Rolling 24h window. Run.created_at lands in the store as
        # a numeric Unix epoch (neomodel's DateTimeProperty serializer), so we
        # compare against a numeric `since_epoch` rather than a DateTime — that
        # would raise "Invalid call signature" in Cypher.
        since_epoch = (datetime.now(UTC) - timedelta(days=1)).timestamp()

        # Tenancy: everything below is scoped to the org's repositories.
        repo_rows, _ = await adb.cypher_query(
            "MATCH (r:Repository {org_uid: $org}) RETURN r.uid, r.name, r.slug",
            {"org": org_uid},
        )
        repos = [row[0] for row in repo_rows]
        repositories_github = len(repos)

        doc_total_rows, _ = await adb.cypher_query(
            "MATCH (d:Doc) WHERE d.repository_uid IN $repos RETURN count(d)",
            {"repos": repos},
        )
        total_docs = doc_total_rows[0][0] if doc_total_rows else 0

        finding_status_rows, _ = await adb.cypher_query(
            "MATCH (f:Finding) WHERE f.kind IN $kinds AND f.repository_uid IN $repos "
            "RETURN f.status AS status, count(f) AS c",
            {"kinds": list(_REAL_FINDING_KINDS), "repos": repos},
        )
        finding_statuses = [
            FindingStatusCount(status=row[0] or "unknown", count=row[1])
            for row in finding_status_rows
        ]
        open_findings = sum(
            c.count for c in finding_statuses if c.status in _OPEN_FINDING_STATUSES
        )

        high_sev_rows, _ = await adb.cypher_query(
            "MATCH (f:Finding) WHERE f.severity IN $sev AND f.status IN $stat "
            "AND f.kind IN $kinds AND f.repository_uid IN $repos RETURN count(f)",
            {
                "sev": list(_HIGH_SEVERITY),
                "stat": list(_OPEN_FINDING_STATUSES),
                "kinds": list(_REAL_FINDING_KINDS),
                "repos": repos,
            },
        )
        high_severity_findings = high_sev_rows[0][0] if high_sev_rows else 0

        # Tags are free-text (JSON list per Finding, stored serialized), so
        # aggregate in Python: dynamic keys derived from the data itself.
        tag_rows, _ = await adb.cypher_query(
            "MATCH (f:Finding) WHERE f.status IN $stat AND f.kind IN $kinds "
            "AND f.repository_uid IN $repos RETURN f.tags",
            {
                "stat": list(_OPEN_FINDING_STATUSES),
                "kinds": list(_REAL_FINDING_KINDS),
                "repos": repos,
            },
        )
        tag_counts: dict[str, int] = {}
        for row in tag_rows:
            raw = row[0]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = []
            for tag in raw or []:
                tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
        finding_tags = [
            FindingTagCount(tag=tag, count=count)
            for tag, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        proposals_rows, _ = await adb.cypher_query(
            "MATCH (f:Finding) WHERE f.kind = 'proposal' AND f.status IN $stat "
            "AND f.repository_uid IN $repos RETURN count(f)",
            {"stat": list(_OPEN_FINDING_STATUSES), "repos": repos},
        )
        proposals = proposals_rows[0][0] if proposals_rows else 0

        runs_rows, _ = await adb.cypher_query(
            "MATCH (r:Run) "
            "WHERE r.created_at IS NOT NULL AND r.created_at >= $since "
            "AND r.repository_uid IN $repos "
            "RETURN count(r)",
            {"since": since_epoch, "repos": repos},
        )
        runs_last_24h = runs_rows[0][0] if runs_rows else 0

        per_repo: list[RepoSummary] = []
        for row in repo_rows:
            uid, name, slug = row[0], row[1] or "", row[2] or ""
            dc_rows, _ = await adb.cypher_query(
                "MATCH (d:Doc) WHERE d.repository_uid = $u RETURN count(d)",
                {"u": uid},
            )
            of_rows, _ = await adb.cypher_query(
                "MATCH (f:Finding) WHERE f.repository_uid = $u AND f.status IN $s "
                "AND f.kind IN $k RETURN count(f)",
                {"u": uid, "s": list(_OPEN_FINDING_STATUSES), "k": list(_REAL_FINDING_KINDS)},
            )
            hf_rows, _ = await adb.cypher_query(
                "MATCH (f:Finding) WHERE f.repository_uid = $u "
                "AND f.severity IN $sev AND f.status IN $s AND f.kind IN $k "
                "RETURN count(f)",
                {
                    "u": uid,
                    "sev": list(_HIGH_SEVERITY),
                    "s": list(_OPEN_FINDING_STATUSES),
                    "k": list(_REAL_FINDING_KINDS),
                },
            )
            pr_rows, _ = await adb.cypher_query(
                "MATCH (f:Finding) WHERE f.repository_uid = $u "
                "AND f.kind = 'proposal' AND f.status IN $s RETURN count(f)",
                {"u": uid, "s": list(_OPEN_FINDING_STATUSES)},
            )
            rr_rows, _ = await adb.cypher_query(
                "MATCH (r:Run) WHERE r.repository_uid = $u "
                "AND r.created_at IS NOT NULL AND r.created_at >= $since "
                "RETURN count(r)",
                {"u": uid, "since": since_epoch},
            )
            per_repo.append(
                RepoSummary(
                    repository_uid=uid,
                    repository_name=name,
                    repository_slug=slug,
                    docs=dc_rows[0][0] if dc_rows else 0,
                    open_findings=of_rows[0][0] if of_rows else 0,
                    high_severity_findings=hf_rows[0][0] if hf_rows else 0,
                    proposals=pr_rows[0][0] if pr_rows else 0,
                    runs_last_24h=rr_rows[0][0] if rr_rows else 0,
                )
            )

        return OverviewMetrics(
            repositories_github=int(repositories_github),
            total_docs=int(total_docs),
            open_findings=int(open_findings),
            high_severity_findings=int(high_severity_findings),
            proposals=int(proposals),
            runs_last_24h=int(runs_last_24h),
            finding_statuses=finding_statuses,
            finding_tags=finding_tags,
            repositories=per_repo,
        )
