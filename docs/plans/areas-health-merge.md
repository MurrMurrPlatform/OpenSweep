# Plan — Make staleness trustworthy, then fold Health into Areas

Two problems, in dependency order. The Health page is not wrong so much as
*narrow* (it reports on Docs only, while Areas are the primary knowledge
structure), and the staleness signal it renders is **unreliable at the source**.
Merging the pages without fixing the source would just spread a bad signal
across a bigger table.

Locked with the user:

- **Staleness fires on default-branch pushes only.** A merged PR is what makes
  an area stale; a feature-branch push is not. Behavior change, no shim.
- **Docs roll into the area row.** Each area row carries a docs column
  (linked + stale counts); docs matching no area collect under "Unassigned".

No backwards compatibility — rename fields, change DTOs, drop dead code. Dev
data is disposable (`infrastructure/dev_reset.py`); reseed rather than migrate.

---

## Findings this plan responds to

Verified against the code, not inferred:

| # | Defect | Location |
|---|---|---|
| F1 | Push freshness has **no branch filter** — any branch marks areas stale. The `ref` is parsed for PR-sync at `:181-182` and ignored here. | `back_end/api/v1/github_webhooks.py:542-551` |
| F2 | Changed paths come from `payload["commits"]`, which GitHub **truncates at 20 commits**; merge commits carry empty file lists. Large merges silently under-report. | `back_end/api/v1/github_webhooks.py:544-547` |
| F3 | **No reconciliation.** Nothing recomputes staleness from git history. Missed delivery / late repo registration / rotated HMAC → permanently and silently wrong. `Repository` has no last-processed sha; no Celery beat tick touches freshness. | `back_end/celery_app.py:57-99` |
| F4 | Failures are swallowed whole. `AreaStaleResult.errors` is computed and discarded; a partial failure renders as "fresh". | `back_end/domains/agents/services/event_triggers.py:194-207` |
| F5 | Non-leaf grouping areas own no `scope_paths`, so they can never be stale — and `AreasView` shows no child rollup for subsystems, so a parent row reads clean over stale leaves. | `back_end/domains/areas/services/area_service.py:621`, `front_end/src/views/AreasView.vue:587-656` |
| F6 | `ignore` / `feature_ignore` areas *are* stamped stale, but the Ignored tab renders no stale dot — invisible state. | `front_end/src/views/AreasView.vue:776-786` |
| F7 | Health page is **docs-only**; Areas appear nowhere in `audit_coverage`. | `back_end/domains/checked/services/checked_service.py:162-203` |
| F8 | `audit_coverage` does `Checked.nodes.all()` + `Doc.nodes.all()` and filters by repo **in Python** — a cross-tenant full scan on every page load. | `back_end/domains/checked/services/checked_service.py:174,182` |
| F9 | Area audit coverage is only reachable via path-overlap (`stamps_for_paths`), only on the detail page, and is O(all repo stamps) *per area* — no batch path for a table. | `back_end/domains/areas/services/area_service.py:396`, `checked_service.py:~275` |
| F10 | No UI affordance to confirm-current. A human who reviews an area and finds it fine must make a no-op edit to clear stale. | `back_end/domains/areas/services/area_freshness.py:57` |
| F11 | `health_grade` / `health_score` are LLM-authored, validated only against an enum, and share the word "health" with an unrelated signal on the same page. | `back_end/domains/platform_tools/upsert_analysis.py:32-75` |

F1–F4 are correctness. F7–F9 are what makes the merge possible. The rest are
carried along.

---

## Phase 1 — Make the stale signal true

Nothing else matters until a merged PR reliably marks its areas stale.

### 1a. Default-branch filter + untruncated paths

`back_end/api/v1/github_webhooks.py:542-551` — replace the inline
`payload["commits"]` walk with a helper that:

- Reads `ref`, strips `refs/heads/`, and **returns early unless it equals
  `repo.default_branch or "main"`**. (The ref is already parsed for PR-sync at
  `:181-182` — hoist that into one place.)
- Resolves changed paths from `compare(before, after)` rather than the commit
  array, falling back to `payload["commits"]` only when `before` is the null
  sha (branch creation) or the compare call fails.

Requires a new provider method — there is no compare today:

- `infrastructure/github_client.py` — add
  `async def compare_commits(owner, repo, base, head) -> list[str]` returning
  changed file paths, paginating `files` (GitHub caps at 300 per page; use the
  `files` array plus `Link` headers, and return a `truncated` flag rather than
  silently short-changing the caller).
- `infrastructure/git_providers/protocol.py:28-48` — add it to the Protocol so
  non-GitHub providers must implement it.

Put the payload→paths extraction in a **pure, unit-testable function** —
`back_end/domains/repositories/services/push_paths.py` with
`changed_paths_from_push(payload, default_branch) -> PushPaths` — mirroring how
`delivery_disposition` (`github_webhooks.py:59`) is already kept pure and
testable. The webhook does I/O; the decision does not.

### 1b. Record what was processed, so gaps are detectable

- `back_end/domains/repositories/models.py` — add
  `freshness_synced_sha = StringProperty(default="")` and
  `freshness_synced_at = DateTimeProperty()`, stamped after a successful
  `refresh_docs_for_change`.
- This is the anchor for 1c and the honest "as of" line in the UI (Phase 3).

### 1c. Reconciliation tick (closes F3)

New Celery beat task alongside the existing six in `back_end/celery_app.py:57-99`:

- `opensweep.repositories.reconcile_freshness`, every 15 min.
- Per active repo: fetch default-branch head sha; if it differs from
  `freshness_synced_sha`, `compare_commits(freshness_synced_sha, head)` and run
  the same `refresh_docs_for_change` path, then re-stamp.
- Empty `freshness_synced_sha` (fresh registration) → stamp the current head
  **without** marking anything stale. A newly registered repo is not
  retroactively stale for all history; it starts from now.
- Idempotent with the webhook by construction: both advance the same cursor.

This is the single highest-value item in the plan. It converts staleness from
"correct if every webhook ever delivered" to "self-healing".

### 1d. Stop swallowing failures (closes F4)

- `event_triggers.refresh_docs_for_change` (`:161`) — return a small result
  (`docs_marked`, `areas_marked`, `errors`) instead of `None`. Keep it
  non-raising; the webhook must still 200.
- Persist a repo-level `freshness_degraded_reason` when errors are non-empty or
  compare was truncated, and surface it in the Phase 3 UI. `file_tree_paths`
  already models exactly this — it returns `(paths, degraded_reason)` and the
  detail view renders `tree_degraded`. Follow that precedent rather than
  inventing a new one.

### 1e. Parent rollup (closes F5)

`area_is_stale` (`back_end/domains/areas/models.py:114`) stays as-is — it is the
leaf truth. Add a **derived rollup** in the Phase 2 service, not on the model: a
non-leaf area is `stale_descendants > 0`. The area detail view already does this
for feature parents (`area_service.py:390-404`); generalize it rather than
duplicating.

---

## Phase 2 — One batched area-health rollup

The merged table needs per-area docs + audit + review state in **one** query.
Today that is O(areas × all repo stamps) (F9) on top of a cross-tenant full scan
(F8).

New `back_end/domains/areas/services/area_health.py`:

```
async def area_health(repository_uid) -> list[AreaHealthDTO]
```

One pass, no N+1:

1. Load once, scoped: `Area.nodes.filter(repository_uid=...)`,
   `Doc.nodes.filter(repository_uid=...)`,
   `Checked.nodes.filter(repository_uid=...)`.
   **Fix F8 while here** — `checked_service.audit_coverage:174,182` must use
   `.filter(repository_uid=...)`, not `.nodes.all()` with a Python filter. That
   is a tenancy smell independent of performance.
2. Build one path→area index from `scope_paths` (reuse `watches_path` from
   `path_matching.py:27` — do not write a second matcher).
3. Resolve each `Checked` stamp to its areas **once** via `covered_paths`, and
   each `Doc` to its areas via `Area.doc_uids` ∪ `watch_paths` overlap.
4. Emit per area: `stale`, `stale_descendants`, `last_reviewed_at`,
   `code_changed_at`, `stale_paths`, `docs_total`, `docs_stale`,
   `spec_state` (`present|missing|stale`), `last_checked`, `outcome`,
   `revision`, `pending_edits`, `file_count`.
5. Docs resolving to no area → an `unassigned` pseudo-row.

Endpoint: `GET /api/v1/repositories/{uid}/areas/health` in
`back_end/api/v1/areas.py`, org-scoped via `require_repo_in_org` like its
neighbours.

`spec_state` note: "spec stale" is not a new axis — a feature leaf is already
treated as needing a spec when it has none *or* is stale
(`runs/services/sweep.py:181-205`). Surface that existing rule, don't invent a
parallel one.

**Deliberately not done here:** no new stored fields, no `content_hash`, no
denormalized stale flag. Staleness stays derived — that is the invariant
`docs/plans/freshness-and-quality-fix-pass.md` established and it should hold.

---

## Phase 3 — Merge Health into Areas

`front_end/src/views/AreasView.vue` keeps its three tabs but each row grows from
a title + chips into a real row backed by `areas/health`:

| Column | Source | Notes |
|---|---|---|
| Area | `key`, `title`, kind badge, tree indent | unchanged rendering |
| Files | `file_count` | today comes from the campaign-areas preview; move it onto the health DTO so one call feeds the table |
| Spec | `spec_state` | missing / stale / ok |
| Docs | `docs_total`, `docs_stale` | "3 · 1 stale" |
| Audit | `last_checked`, `outcome`, `revision` | the Health page's actual content, per area |
| Review | `stale`, `stale_descendants`, `last_reviewed_at` | amber dot keeps `areaStaleTitle` (`front_end/src/lib/areas.ts:33`); parent rows show the rollup count |
| — | row actions | Audit now · **Confirm current** · Edit |

Also:

- **Summary tiles** move over from `HealthView.vue:286-291`, recomputed against
  areas: total / stale / never-audited / fresh. Keep them in the existing health
  strip (`AreasView.vue:434-477`) rather than adding a second stat row — that
  strip already carries partition drift, and one header is enough.
- **Stale dot on the Ignored tab** (closes F6). An ignore area whose files moved
  is exactly the case where the ignore reason may no longer hold.
- **Confirm-current button** (closes F10) → new
  `POST /api/v1/areas/{uid}/confirm-current` wrapping the existing
  `area_freshness.confirm_area_current:57`. The service exists and is already
  agent-reachable; it just has no human door. Maintainer role, matching
  `PATCH /areas/{uid}`.
- **"As of" line** — render `freshness_synced_at` and any
  `freshness_degraded_reason` (1d) in the page header. If the signal is
  degraded, the page must say so rather than showing confident green.
- **Sort/filter by stale**, which the current tree cannot do. Default sort:
  stale → never-audited → fresh, mirroring `HealthView.vue:273-284`.

### Retiring the Health page

- Delete `front_end/src/views/HealthView.vue`; drop the route
  (`front_end/src/router/index.ts:66-67`) and the nav entry
  (`front_end/src/composables/useNavSections.ts:47`). Redirect `health` → `areas`
  so existing links survive.
- The audit / deep-scan / scheduled-audit dialogs (`HealthView.vue:65-235`) move
  onto Areas — they are repo-scoped actions, not doc-scoped, and belong wherever
  the coverage table lives.
- **Rename `health_grade`/`health_score`** on the Analysis node to
  `report_grade`/`report_score` (F11). Two unrelated meanings of "health" on one
  page was tolerable; on the merged page it is actively confusing. Touches
  `domains/analysis/models.py:41-42`, `schemas.py:83-84`,
  `platform_tools/upsert_analysis.py:32-75`, `types/api.ts:1925-1926`.
- `GET /repositories/{uid}/freshness` (`api/v1/freshness.py:21`) stays — the Docs
  page still needs per-doc coverage. It is no longer the Health page's backend.

---

## Phase 4 — Tests

The freshness path is exactly where silent wrongness lives, so the pure
functions carry the weight.

**Unit, no Neo4j:**
- `changed_paths_from_push` — non-default branch → empty; branch creation
  (null `before`) → falls back to commits; >20 commits → compare wins; malformed
  payload → empty, no raise.
- Area health rollup over fixture dicts: parent rollup, unassigned docs,
  ignore-area staleness, `spec_state` transitions.

**Integration (existing harness, `back_end/tests/test_freshness_integration.py`):**
- Push to default branch marks the covering area stale; push to a feature branch
  does **not** (the F1 regression test).
- Reconciliation tick catches up a repo that missed a delivery, and marks
  nothing on a first-time repo with an empty cursor.
- `confirm_area_current` via the new endpoint clears stale and advances
  `last_reviewed_at` — extend the existing coverage at `:156-172`.
- `area_health` returns one row per area with no N+1 (assert the query count).

---

## Sequencing

Phase 1 is independently shippable and worth landing alone — it fixes the signal
whether or not the pages ever merge. Phase 2 depends on 1b only for the "as of"
field. Phase 3 depends on 2. Phase 4 lands with each phase, not at the end.

Suggested split: **PR 1** = 1a–1d (webhook correctness + reconciliation),
**PR 2** = 1e + Phase 2 (rollup service + endpoint), **PR 3** = Phase 3 (UI merge
+ Health retirement).
