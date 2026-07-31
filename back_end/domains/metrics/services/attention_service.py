"""Per-repo "needs attention" rollup — the workspace Overview's next-actions
panel.

Unlike notifications (event-driven inbox over the audit stream), this is a
CURRENT-STATE aggregation: every query below asks "what is outstanding right
now", so acting on an item makes it disappear on the next fetch without any
read/dismiss bookkeeping.

Datetime caveat (same as metrics_service): DateTimeProperty lands in the
store as a numeric Unix epoch, so time comparisons use epoch floats and rows
convert back with `_dt()`.
"""

from datetime import UTC, datetime, timedelta

from neomodel import adb

from domains.metrics.schemas import AttentionItem, RepoAttention
from domains.metrics.services.metrics_service import (
    _HIGH_SEVERITY,
    _OPEN_FINDING_STATUSES,
    _REAL_FINDING_KINDS,
)

# Fixed urgency ordering: halted state first, then merge-path blockers that
# name a human, then broken automation, then approvals, then backlog-shaped
# aggregates. 1 = most urgent.
KIND_PRIORITY: dict[str, int] = {
    "kill_switch_engaged": 1,
    "verdict_needs_human": 2,
    "pr_fix_rounds_exhausted": 3,
    "pr_waive_requested": 4,
    "pr_ci_red": 5,
    "run_needs_attention": 6,
    "thread_plan_awaiting_approval": 7,
    "thread_ready_for_review": 8,
    "area_edits_pending": 9,
    "epic_proposals_pending": 10,
    "high_severity_findings": 11,
    "stale_areas": 12,
}

# Per-entity kinds are capped so one runaway source can't drown the panel;
# aggregate kinds are a single item with a count.
PER_KIND_CAP = 20

_RUN_ATTENTION_STATUSES = ("failed", "limit_exceeded", "paused_quota")
_ENDED_THREAD_PHASES = ("done", "abandoned")


def _dt(value) -> datetime | None:
    """Epoch float (or datetime) from a Cypher row → aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError):
        return None


def assemble(items: list[AttentionItem], repository_uid: str) -> RepoAttention:
    """Pure assembly: sort by (priority, newest first) and roll up counts."""
    ordered = sorted(
        items,
        key=lambda i: (
            i.priority,
            -(i.created_at.timestamp() if i.created_at else 0.0),
        ),
    )
    counts: dict[str, int] = {}
    for item in ordered:
        counts[item.kind] = counts.get(item.kind, 0) + item.count
    return RepoAttention(
        repository_uid=repository_uid,
        generated_at=datetime.now(UTC),
        total=sum(counts.values()),
        counts_by_kind=counts,
        items=ordered,
    )


class AttentionService:
    async def repo_attention(self, repository_uid: str) -> RepoAttention:
        items: list[AttentionItem] = []
        u = repository_uid

        items += await self._kill_switch(u)
        items += await self._verdicts_needs_human(u)
        items += await self._prs_fix_rounds_exhausted(u)
        items += await self._prs_waive_requested(u)
        items += await self._prs_ci_red(u)
        items += await self._runs_needing_attention(u)
        items += await self._threads(u)
        items += await self._aggregates(u)

        return assemble(items, repository_uid)

    # ── Per-entity sources ───────────────────────────────────────────────────

    async def _kill_switch(self, u: str) -> list[AttentionItem]:
        repo_rows, _ = await adb.cypher_query(
            "MATCH (r:Repository {uid: $u}) RETURN r.kill_switch_active", {"u": u}
        )
        global_rows, _ = await adb.cypher_query(
            "MATCH (p:PlatformConfig) RETURN p.global_kill_switch LIMIT 1", {}
        )
        repo_on = bool(repo_rows and repo_rows[0][0])
        global_on = bool(global_rows and global_rows[0][0])
        if not repo_on and not global_on:
            return []
        which = (
            "Repository and global kill switches are engaged"
            if repo_on and global_on
            else "Repository kill switch is engaged"
            if repo_on
            else "Global kill switch is engaged"
        )
        return [
            AttentionItem(
                kind="kill_switch_engaged",
                priority=KIND_PRIORITY["kill_switch_engaged"],
                title="Kill switch engaged",
                description=f"{which} — no new runs will be dispatched.",
                subject_type="Repository",
                subject_uid=u,
            )
        ]

    async def _verdicts_needs_human(self, u: str) -> list[AttentionItem]:
        # Only verdicts at the PR's CURRENT head count — a push invalidates.
        # Superseded verdicts were replaced by a verification-adjusted one.
        rows, _ = await adb.cypher_query(
            "MATCH (pr:PullRequest {repository_uid: $u, state: 'open'}) "
            "MATCH (v:Verdict {repository_uid: $u, result: 'needs_human'}) "
            "WHERE v.pull_request_uid = pr.uid AND v.sha = pr.head_sha "
            "AND coalesce(v.verification_status, '') <> 'superseded' "
            "RETURN pr.uid, pr.github_number, pr.title, max(v.created_at) "
            "ORDER BY max(v.created_at) DESC LIMIT $cap",
            {"u": u, "cap": PER_KIND_CAP},
        )
        return [
            AttentionItem(
                kind="verdict_needs_human",
                priority=KIND_PRIORITY["verdict_needs_human"],
                title=f"PR #{row[1]} needs a human decision",
                description=row[2] or "",
                subject_type="PullRequest",
                subject_uid=row[0],
                created_at=_dt(row[3]),
            )
            for row in rows
        ]

    async def _prs_fix_rounds_exhausted(self, u: str) -> list[AttentionItem]:
        rows, _ = await adb.cypher_query(
            "MATCH (pr:PullRequest {repository_uid: $u, state: 'open', "
            "fix_rounds_exhausted: true}) "
            "RETURN pr.uid, pr.github_number, pr.title, pr.updated_at "
            "ORDER BY pr.updated_at DESC LIMIT $cap",
            {"u": u, "cap": PER_KIND_CAP},
        )
        return [
            AttentionItem(
                kind="pr_fix_rounds_exhausted",
                priority=KIND_PRIORITY["pr_fix_rounds_exhausted"],
                title=f"PR #{row[1]} exhausted its fix rounds",
                description=row[2] or "",
                subject_type="PullRequest",
                subject_uid=row[0],
                created_at=_dt(row[3]),
            )
            for row in rows
        ]

    async def _prs_waive_requested(self, u: str) -> list[AttentionItem]:
        # Same predicate as delivery's waive_requested_count: a request reason
        # not yet approved by a human (state != 'waived').
        rows, _ = await adb.cypher_query(
            "MATCH (pr:PullRequest {repository_uid: $u, state: 'open'}) "
            "MATCH (res:FindingResolution {repository_uid: $u}) "
            "WHERE res.pull_request_uid = pr.uid "
            "AND res.waive_requested_reason <> '' AND res.state <> 'waived' "
            "RETURN pr.uid, pr.github_number, pr.title, count(res), max(res.updated_at) "
            "ORDER BY max(res.updated_at) DESC LIMIT $cap",
            {"u": u, "cap": PER_KIND_CAP},
        )
        return [
            AttentionItem(
                kind="pr_waive_requested",
                priority=KIND_PRIORITY["pr_waive_requested"],
                title=f"PR #{row[1]} has {row[3]} waiver request{'s' if row[3] != 1 else ''}",
                description=row[2] or "",
                subject_type="PullRequest",
                subject_uid=row[0],
                count=int(row[3]),
                created_at=_dt(row[4]),
            )
            for row in rows
        ]

    async def _prs_ci_red(self, u: str) -> list[AttentionItem]:
        # Drafts are excluded: red CI on work-in-progress is expected and
        # would drown the panel in noise.
        rows, _ = await adb.cypher_query(
            "MATCH (pr:PullRequest {repository_uid: $u, state: 'open', "
            "ci_state: 'red', draft: false}) "
            "RETURN pr.uid, pr.github_number, pr.title, pr.updated_at "
            "ORDER BY pr.updated_at DESC LIMIT $cap",
            {"u": u, "cap": PER_KIND_CAP},
        )
        return [
            AttentionItem(
                kind="pr_ci_red",
                priority=KIND_PRIORITY["pr_ci_red"],
                title=f"PR #{row[1]} has red CI",
                description=row[2] or "",
                subject_type="PullRequest",
                subject_uid=row[0],
                created_at=_dt(row[3]),
            )
            for row in rows
        ]

    async def _runs_needing_attention(self, u: str) -> list[AttentionItem]:
        since = (datetime.now(UTC) - timedelta(days=1)).timestamp()
        rows, _ = await adb.cypher_query(
            "MATCH (r:Run {repository_uid: $u}) "
            "WHERE r.status IN $statuses AND r.created_at IS NOT NULL "
            "AND r.created_at >= $since "
            "RETURN r.uid, r.title, r.playbook, r.status, r.created_at "
            "ORDER BY r.created_at DESC LIMIT $cap",
            {"u": u, "statuses": list(_RUN_ATTENTION_STATUSES), "since": since,
             "cap": PER_KIND_CAP},
        )
        labels = {
            "failed": "failed",
            "limit_exceeded": "hit its run-policy ceiling",
            "paused_quota": "is paused on a provider quota",
        }
        return [
            AttentionItem(
                kind="run_needs_attention",
                priority=KIND_PRIORITY["run_needs_attention"],
                title=f"{(row[2] or 'run').capitalize()} run {labels.get(row[3], row[3])}",
                description=row[1] or "",
                subject_type="Run",
                subject_uid=row[0],
                created_at=_dt(row[4]),
            )
            for row in rows
        ]

    async def _threads(self, u: str) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        for kind, predicate, title in (
            ("thread_plan_awaiting_approval", "t.plan_state = 'drafted'",
             "Plan drafted, awaiting approval"),
            ("thread_ready_for_review", "t.ready_for_review = true",
             "Thread ready for review"),
        ):
            rows, _ = await adb.cypher_query(
                f"MATCH (t:Thread {{repository_uid: $u}}) "
                f"WHERE {predicate} AND NOT t.phase IN $ended "
                f"OPTIONAL MATCH (tk:Ticket {{uid: t.subject_ticket_uid}}) "
                f"RETURN t.uid, coalesce(tk.title, ''), t.updated_at "
                f"ORDER BY t.updated_at DESC LIMIT $cap",
                {"u": u, "ended": list(_ENDED_THREAD_PHASES), "cap": PER_KIND_CAP},
            )
            items += [
                AttentionItem(
                    kind=kind,
                    priority=KIND_PRIORITY[kind],
                    title=title,
                    description=row[1],
                    subject_type="Thread",
                    subject_uid=row[0],
                    created_at=_dt(row[2]),
                )
                for row in rows
            ]
        return items

    # ── Aggregate sources (one item with a count each) ───────────────────────

    async def _aggregates(self, u: str) -> list[AttentionItem]:
        items: list[AttentionItem] = []

        async def count(query: str, params: dict) -> int:
            rows, _ = await adb.cypher_query(query, params)
            return int(rows[0][0]) if rows else 0

        area_edits = await count(
            "MATCH (e:AreaEdit {repository_uid: $u, status: 'pending'}) RETURN count(e)",
            {"u": u},
        )
        if area_edits:
            items.append(AttentionItem(
                kind="area_edits_pending",
                priority=KIND_PRIORITY["area_edits_pending"],
                title=f"{area_edits} area edit{'s' if area_edits != 1 else ''} awaiting review",
                description="Agent-proposed changes to the area map need a human decision.",
                subject_type="AreaEdit",
                count=area_edits,
            ))

        epics = await count(
            "MATCH (p:EpicProposal {repository_uid: $u, status: 'proposed'}) RETURN count(p)",
            {"u": u},
        )
        if epics:
            items.append(AttentionItem(
                kind="epic_proposals_pending",
                priority=KIND_PRIORITY["epic_proposals_pending"],
                title=f"{epics} epic proposal{'s' if epics != 1 else ''} awaiting review",
                description="Proposed ticket groupings need approval or rejection.",
                subject_type="EpicProposal",
                count=epics,
            ))

        high_sev = await count(
            "MATCH (f:Finding {repository_uid: $u}) "
            "WHERE f.severity IN $sev AND f.status IN $stat AND f.kind IN $kinds "
            "RETURN count(f)",
            {"u": u, "sev": list(_HIGH_SEVERITY),
             "stat": list(_OPEN_FINDING_STATUSES), "kinds": list(_REAL_FINDING_KINDS)},
        )
        if high_sev:
            items.append(AttentionItem(
                kind="high_severity_findings",
                priority=KIND_PRIORITY["high_severity_findings"],
                title=f"{high_sev} high-severity finding{'s' if high_sev != 1 else ''} open",
                description="Critical or high findings still outstanding.",
                subject_type="Finding",
                count=high_sev,
            ))

        # Mirrors areas.models.area_is_stale: code changed under scope_paths
        # since the area was last reviewed (never-changed areas are not stale).
        stale = await count(
            "MATCH (a:Area {repository_uid: $u, enabled: true}) "
            "WHERE a.code_changed_at IS NOT NULL "
            "AND (a.last_reviewed_at IS NULL OR a.code_changed_at > a.last_reviewed_at) "
            "RETURN count(a)",
            {"u": u},
        )
        if stale:
            items.append(AttentionItem(
                kind="stale_areas",
                priority=KIND_PRIORITY["stale_areas"],
                title=f"{stale} area{'s' if stale != 1 else ''} stale",
                description="Code changed since these areas were last reviewed.",
                subject_type="Area",
                count=stale,
            ))

        return items


attention_service = AttentionService()
