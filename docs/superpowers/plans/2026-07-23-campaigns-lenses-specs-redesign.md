# Campaigns / Lenses / Specs redesign + Areas UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace rigid campaign templates with single-axis campaigns (subsystem / feature / global) + an "audit everything" batch, decouple lens execution mode from lens identity, make feature specs editable (manual + AI), and fix the Areas/Campaign UI.

**Architecture:** A campaign becomes `kind → coverage (multi-select node keys) → selection (all|stale|rotation) → depth (effort) → lenses`. The campaign **kind** sets the execution mode (subsystem/feature = per-area parts, global = whole-repo sweeps), so `lens.scope` is deleted. `batch` is a thin parent that spawns three child campaigns and rolls up their digests. Specs stay `AreaEdit`-reviewed; a one-shot "revise" run and the existing global chat widget both propose edits. UI extracts one shared tree helper and adds a plan stat-header + live create preview.

**Tech Stack:** Python 3 / FastAPI / neomodel (async, Neo4j) / Celery; Vue 3 + Vite + Pinia + shadcn; pytest; ruff.

## Global Constraints

- **Two-repo rule:** all of this is shared product code — implement here (`opensweep`), never in the cloud overlay. Add extension points, no `if cloud:` branches. (CLAUDE.md)
- **Neo4j migrations** live in `back_end/migrations/m<NNNN>_<slug>.py`, VERSION contiguous. Highest existing = **12**, so new files are **m0013**, **m0014**. Contract: `VERSION:int`, `NAME:str`, `SCHEMA_UP/SCHEMA_DOWN:list[str]`, `UP/DOWN:list[str]`; auto-discovered by filename sort (`back_end/migrations/__init__.py`).
- **Planner stays pure** (`domains/campaigns/services/planner.py`): no DB, unit-testable. `campaign_service` supplies loaded docs/tree/lenses.
- **neomodel `save()` writes every property** — always reload-before-append for event/parts mutations (existing `record_event` discipline).
- **Back-compat:** historical campaigns (with `template`, no `kind`) must still render read-only; never re-plan/re-run them.
- Run tests from `back_end/`: `pytest back_end/tests/…`. Lint: `ruff check`.

---

## File structure (created / modified)

**Backend — model + planner (Stage 1)**
- `domains/lenses/models.py` — drop `scope` property.
- `domains/lenses/services/seed_lenses.py` — drop `scope` from seed rows; keep `global_agent_key`.
- `domains/lenses/services/lens_service.py` — remove `scope` from DTO + sort key; add `DEFAULT_LENSES_BY_KIND` + `default_lens_keys(kind)`.
- `domains/lenses/schemas.py` — drop `scope` from `LensDTO`.
- `domains/campaigns/services/planner.py` — `filter_by_prefix`→add `filter_by_keys`; rewrite `build_plan` to kind-dispatch.
- `back_end/migrations/m0014_drop_lens_scope.py` — new.

**Backend — campaign model + service + batch + API + scheduling (Stage 2)**
- `domains/campaigns/models.py` — add `kind`, `coverage_keys`, `selection`, `parent_uid`, `child_uids`.
- `domains/campaigns/schemas.py` — add same to `CampaignDTO` + `CreateCampaignRequest`.
- `domains/campaigns/services/campaign_service.py` — kind-aware `_plan_parts`, `create`, `preview`; `plan_summary.total_runs` + by-kind.
- `domains/campaigns/services/batch.py` — new: spawn children, roll-up summary.
- `domains/campaigns/services/tick.py` — batch aggregation in `tick_campaigns`.
- `api/v1/campaigns.py` — accept new create fields; add `campaign-plan-preview`.
- `domains/agents/services/schedule_scanner.py` + `scheduled_agent_service.py` — translate `template`→`kind` (legacy) / accept `kind`.
- `back_end/migrations/m0013_campaign_kind_fields.py` — new (backfill `kind`/`coverage_keys`).

**Backend — specs editing (Stage 3)**
- `domains/runs/services/sweep.py` — add `revise_area_spec(area_uid, instruction)`.
- `domains/agents/services/seed_agent_bases.py` — add `revise-spec` agent base (or reuse `generate-specs`).
- `api/v1/areas.py` — add `POST /areas/{uid}/revise-spec`.
- `domains/runs/services/chat_context.py` + `front_end usePageContext` — publish current `area` as chat subject.

**Frontend — UI (Stage 4)**
- `front_end/src/lib/treeRows.ts` — new shared tree helper (extracted).
- `front_end/src/views/AreasView.vue` — Features tab uses tree helper; `partitionRows` refactored onto it.
- `front_end/src/views/AreaDetailView.vue` — sub-features tree; spec editor + "revise with AI".
- `front_end/src/views/CampaignDetailView.vue` — collapsible/clickable part rows; plan stat-header; batch roll-up.
- `front_end/src/components/campaigns/NewCampaignDialog.vue` — kind/coverage/selection controls + live preview.
- `front_end/src/stores/campaignStore.ts` — `previewPlan()`; `areaStore.ts` — `reviseSpec()`.
- `front_end/src/composables/usePageContext.ts` — add `area-detail` subject.

---

## Stage 1 — Lens model + planner kinds (foundation)

### Task 1: Delete `lens.scope`, add per-kind default lenses

**Files:**
- Modify: `domains/lenses/models.py` (Lens node), `domains/lenses/services/seed_lenses.py:303` + seed rows, `domains/lenses/services/lens_service.py:24,44`, `domains/lenses/schemas.py`
- Create: `back_end/migrations/m0014_drop_lens_scope.py`
- Test: `back_end/tests/test_lens_seeds.py` (update), `back_end/tests/test_lens_defaults.py` (new)

**Interfaces:**
- Produces: `lens_service.DEFAULT_LENSES_BY_KIND: dict[str, tuple[str,...]]`, `lens_service.default_lens_keys(kind: str) -> list[str]`. `Lens` no longer has `.scope`. `LensDTO` no longer has `scope`. `global_agent_key` retained.

- [ ] **Step 1: Write failing test** for `default_lens_keys`.
```python
# back_end/tests/test_lens_defaults.py
from domains.lenses.services import lens_service

def test_default_lens_keys_by_kind():
    assert "implementation-gaps" in lens_service.default_lens_keys("feature")
    assert "bugs" in lens_service.default_lens_keys("subsystem")
    # global defaults are exactly the lenses that have a global_agent_key
    assert set(lens_service.default_lens_keys("global")) == {
        "architecture-review", "implementation-gaps",
    }
    assert lens_service.default_lens_keys("nonsense") == []
```
- [ ] **Step 2: Run** `pytest back_end/tests/test_lens_defaults.py -v` → FAIL (no attribute).
- [ ] **Step 3:** Add to `lens_service.py`:
```python
DEFAULT_LENSES_BY_KIND: dict[str, tuple[str, ...]] = {
    "subsystem": (
        "bugs", "security", "performance", "error-handling",
        "legacy-patterns", "refactor-opportunities", "simplification", "test-gaps",
    ),
    "feature": ("implementation-gaps",),
    "global": ("architecture-review", "implementation-gaps"),
}

def default_lens_keys(kind: str) -> list[str]:
    return list(DEFAULT_LENSES_BY_KIND.get(kind, ()))
```
- [ ] **Step 4:** Remove `scope` from `Lens` (models.py), `LensDTO` (schemas.py), `lens_service` to_dto (`:24`) + `list_lenses` sort (`:44` → sort by `key` only), and `seed_lenses.py` (`:303` `_current_values` + each seed row's `"scope"`). Keep `global_agent_key`.
- [ ] **Step 5:** Update `back_end/tests/test_lens_seeds.py` — replace `s["scope"] == "global"` assertions with `bool(s["global_agent_key"])` (global lenses are those with an agent key).
- [ ] **Step 6:** Create migration:
```python
# back_end/migrations/m0014_drop_lens_scope.py
VERSION = 14
NAME = "drop-lens-scope"
SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []
UP: list[str] = ["MATCH (l:Lens) REMOVE l.scope"]
DOWN: list[str] = []  # value drop; re-seed restores defaults
```
- [ ] **Step 7:** Run `pytest back_end/tests/test_lens_defaults.py back_end/tests/test_lens_seeds.py -v` → PASS. `ruff check domains/lenses`.
- [ ] **Step 8: Commit** `feat(lenses): drop scope, add per-kind default lens map`.

### Task 2: `filter_by_keys` (multi-select coverage)

**Files:** Modify `domains/campaigns/services/planner.py` (add near `filter_by_prefix:514`); Test `back_end/tests/test_campaign_planner.py`.

**Interfaces:**
- Produces: `planner.filter_by_keys(areas: list[dict], keys: list[str]) -> list[dict]` — an area is kept if its `area_key` equals or nests under **any** key in `keys` (via `child_key_prefix_of`); empty `keys` keeps everything; areas without `area_key` survive only on empty `keys` (same rule as `filter_by_prefix`).

- [ ] **Step 1: Write failing test.**
```python
def test_filter_by_keys_multi_select():
    areas = [
        {"area_key": "backend/delivery/convergence", "title": "a"},
        {"area_key": "backend/runs", "title": "b"},
        {"area_key": "frontend/views", "title": "c"},
        {"area_key": "", "title": "remainder"},
    ]
    from domains.campaigns.services import planner
    got = {a["title"] for a in planner.filter_by_keys(areas, ["backend/delivery", "frontend/views"])}
    assert got == {"a", "c"}
    assert len(planner.filter_by_keys(areas, [])) == 4  # empty = all
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (mirror `filter_by_prefix`, iterate keys):
```python
def filter_by_keys(areas: list[dict], keys: list[str]) -> list[dict]:
    ks = [k for k in (str(k or "").strip() for k in keys) if k]
    if not ks:
        return list(areas)
    def _match(key: str) -> bool:
        return bool(key) and any(
            key == k or child_key_prefix_of(k, key) for k in ks
        )
    return [a for a in areas if _match(str(a.get("area_key") or ""))]
```
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(planner): filter_by_keys for multi-select coverage`.

### Task 3: Kind-dispatched `build_plan`

**Files:** Modify `domains/campaigns/services/planner.py:579-652` (`build_plan`); Test `back_end/tests/test_campaign_planner.py`.

**Interfaces:**
- Produces: `planner.build_plan(kind: str, areas: list[dict], lenses: list[dict], *, selection: str = "all", k: int = 3, path_recency=None, feature_areas=None) -> list[dict]`.
  - `kind="subsystem"`: one `kind="area"` part per area, `lens_keys` = all enabled passed-in lens keys; `selection` filters (`all`=every area; `stale`=areas flagged stale; `rotation`=k least-recently-covered via existing `_area_recency`).
  - `kind="feature"`: one `kind="feature"` part per feature leaf in `feature_areas`, `lens_keys` = passed lens keys (default `implementation-gaps`); `selection` `all`=every leaf, `stale`/`rotation`=`fa["stale"]` only.
  - `kind="global"`: one `kind="global"` part per passed lens (each must have `global_agent_key`).
  - `kind="batch"`: returns `[]` (handled by `batch.py`).
  - `_part(...)` helper (`:552`) reused unchanged; `idx` reassigned sequentially.

- [ ] **Step 1: Write failing tests** (subsystem/all, subsystem/rotation-k, feature/all, feature/stale, global one-per-lens, batch empty). Example:
```python
def test_build_plan_subsystem_all_one_part_per_area_all_lenses():
    areas = [{"title": "A", "area_key": "backend/a", "scope_paths": ["backend/a"], "file_count": 10}]
    lenses = [{"key": "bugs", "enabled": True}, {"key": "security", "enabled": True}]
    parts = planner.build_plan("subsystem", areas, lenses, selection="all")
    assert [p["kind"] for p in parts] == ["area"]
    assert parts[0]["lens_keys"] == ["bugs", "security"]

def test_build_plan_global_one_part_per_lens():
    lenses = [{"key": "architecture-review", "enabled": True, "global_agent_key": "architecture-review"}]
    parts = planner.build_plan("global", [], lenses, selection="all")
    assert [p["kind"] for p in parts] == ["global"]

def test_build_plan_batch_is_empty():
    assert planner.build_plan("batch", [], [], selection="all") == []
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the kind dispatch, reusing `_area_recency` for rotation and the existing `_part`/`_feature_parts`/`_global_part` builders (drop the old `template`/`focus_lens`/global-alongside logic — kinds are now separate).
- [ ] **Step 4: Run** all of `back_end/tests/test_campaign_planner.py` → PASS (update any legacy `template`-based tests to the new signature). **Step 5: Commit** `feat(planner): kind-dispatched build_plan`.

---

## Stage 2 — Campaign model, batch, API, scheduling

### Task 4: Campaign model + DTO + request fields (+ migration)

**Files:** Modify `domains/campaigns/models.py`, `domains/campaigns/schemas.py`; Create `back_end/migrations/m0013_campaign_kind_fields.py`; Test `back_end/tests/test_campaign_models.py`.

**Interfaces:**
- Produces: `Campaign.kind` (`subsystem|feature|global|batch`, indexed, default `subsystem`), `.coverage_keys` (JSON list, default `[]`), `.selection` (`all|stale|rotation`, default `all`), `.parent_uid` (str, default ""), `.child_uids` (JSON list). `CampaignDTO`/`CreateCampaignRequest` gain `kind`, `coverage_keys`, `selection` (keep `template`, `area_prefix`, `k` for back-compat). New constant `CAMPAIGN_KINDS = {"subsystem","feature","global","batch"}`, `CAMPAIGN_SELECTIONS = {"all","stale","rotation"}`.

- [ ] **Step 1:** Test that a `Campaign(kind="feature", selection="stale", coverage_keys=["x"])` round-trips via `to_dto`; and `CreateCampaignRequest(kind="global")` validates.
- [ ] **Step 2: Run** → FAIL. **Step 3:** Add properties/fields + constants; extend `to_dto` (`campaign_service.py:30`).
- [ ] **Step 4:** Migration backfills legacy rows so old campaigns render:
```python
# m0013_campaign_kind_fields.py
VERSION = 13
NAME = "campaign-kind-fields"
SCHEMA_UP = ["CREATE INDEX campaign_kind IF NOT EXISTS FOR (c:Campaign) ON (c.kind)"]
SCHEMA_DOWN = ["DROP INDEX campaign_kind IF EXISTS"]
UP = [
    "MATCH (c:Campaign) WHERE c.kind IS NULL "
    "SET c.kind = CASE c.template WHEN 'full' THEN 'batch' ELSE 'subsystem' END",
    "MATCH (c:Campaign) WHERE c.selection IS NULL "
    "SET c.selection = CASE c.template WHEN 'rotation' THEN 'rotation' ELSE 'all' END",
    "MATCH (c:Campaign) WHERE c.coverage_keys IS NULL "
    "SET c.coverage_keys = CASE WHEN c.area_prefix IS NULL OR c.area_prefix = '' "
    "THEN [] ELSE [c.area_prefix] END",
]
DOWN = []
```
- [ ] **Step 5: Run** `pytest back_end/tests/test_campaign_models.py` → PASS. **Step 6: Commit** `feat(campaigns): kind/coverage/selection model + migration`.

### Task 5: Kind-aware planning in `campaign_service`

**Files:** Modify `domains/campaigns/services/campaign_service.py` (`_plan_parts:264`, `_plan_areas:192`, `preview_areas:369`, `create:417`); Test `back_end/tests/test_campaign_planner.py` / a new `test_campaign_plan_service.py` (with fakes).

**Interfaces:**
- Produces: `_plan_parts(repository_uid, *, kind, coverage_keys, selection, lens_keys, k)` returns `(parts, degraded_reason, source, plan_summary)`. `plan_summary` gains `total_runs` and `by_kind` (`{"area":n,"feature":n,"global":n}`). Coverage filtering uses `planner.filter_by_keys`; lens defaulting uses `lens_service.default_lens_keys(kind)` when `lens_keys` empty.

- [ ] **Step 1:** Test (fakes for repo/areas/lenses) that a `feature` plan yields only feature parts, a `global` plan yields one part per global lens, `total_runs == len(parts)`, and empty `lens_keys` falls back to `default_lens_keys(kind)`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3:** Rewrite `_plan_parts`: branch on `kind`; for subsystem use `areas_from_map`→`bundle_siblings`→`filter_by_keys`; for feature filter `feature_areas` by keys; for global skip partition and pass global lenses; call `planner.build_plan(kind, …, selection=…)`. Compute `plan_summary` incl. `total_runs`/`by_kind`. Keep `degraded_reason` handling.
- [ ] **Step 4:** Update `create()` to read `req.kind/coverage_keys/selection` (translate legacy `template`→kind when `kind` absent: `full→batch`, `rotation→subsystem`+`selection=rotation`, `focused→subsystem`; `area_prefix`→`coverage_keys`). For `kind=batch`, delegate to `batch.create_batch` (Task 6) instead of building parts.
- [ ] **Step 5: Run** planner + service tests → PASS. **Step 6: Commit** `feat(campaigns): kind-aware planning + plan_summary totals`.

### Task 6: Batch campaigns (`batch.py`)

**Files:** Create `domains/campaigns/services/batch.py`; Modify `tick.py:241` (`tick_campaigns`) + `finalize.py` reuse; Test `back_end/tests/test_campaign_batch.py`.

**Interfaces:**
- Produces:
  - `batch.create_batch(repository_uid, req, *, created_by, trigger_provenance) -> Campaign` — creates the parent (`kind="batch"`, no parts) + three child campaigns (`subsystem`/`feature`/`global`) sharing `effort`/`selection`, each with `parent_uid=parent.uid` and default per-kind lenses; parent `child_uids=[…]`.
  - `batch.launch_batch(parent) -> None` — launch each child (`campaign_service.launch`), parent → `running`.
  - `batch.aggregate_batch(parent) -> bool` — when all children terminal, build `parent.summary` = `{children:[{uid,kind,status,counts}], totals:{…}}` from each child's `summary`, set parent → `done`; returns True when aggregated.

- [ ] **Step 1:** Test `create_batch` makes 3 children with distinct kinds + `parent_uid`; `aggregate_batch` returns False while a child runs and True + parent `done` once all children terminal, summing child `summary.counts.total`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** Implement using existing `campaign_service.create` (per child kind) + `record_event`. **Step 4:** In `tick_campaigns` loop, after per-campaign ticks, call `batch.aggregate_batch` for each running `kind="batch"` parent.
- [ ] **Step 5: Run** `pytest back_end/tests/test_campaign_batch.py` → PASS. **Step 6: Commit** `feat(campaigns): audit-everything batch fan-out + roll-up`.

### Task 7: API + scheduled-agent wiring

**Files:** Modify `api/v1/campaigns.py` (create body, new preview), `domains/agents/services/schedule_scanner.py:108`, `scheduled_agent_service.py:320`; Test `back_end/tests/test_campaigns_api.py`, `test_celery_schedule.py`.

**Interfaces:**
- Produces: `POST /repositories/{uid}/campaigns` accepts `kind/coverage_keys/selection`; new `POST /repositories/{uid}/campaign-plan-preview` (body = create request) → `{total_runs, by_kind, areas, uncovered_files, oversized, degraded}` via `campaign_service.preview_plan` (runs the pure planner, no persist). Scheduled `run-campaign` binding target may carry `kind/coverage_keys/selection` (falls back to `template` translation).

- [ ] **Step 1:** API test: create with `{"kind":"global"}` returns a campaign whose parts are all `kind=global`; plan-preview returns matching `total_runs` without creating a campaign.
- [ ] **Step 2: Run** → FAIL. **Step 3:** Add `campaign_service.preview_plan` (share `_plan_parts` sans save) + endpoint; widen `CreateCampaignRequest`; update `schedule_scanner` to pass `kind`/`selection` when present. **Step 4:** Update seeded binding target (`scheduled_agent_service.py:335`) to `{"kind":"subsystem","selection":"rotation","k":3}`.
- [ ] **Step 5: Run** `pytest back_end/tests/test_campaigns_api.py back_end/tests/test_celery_schedule.py` → PASS. **Step 6: Commit** `feat(api): kind-based campaign create + plan preview + scheduling`.

---

## Stage 3 — Specs as editable objects

### Task 8: One-shot `revise_area_spec`

**Files:** Modify `domains/runs/services/sweep.py` (add sibling of `run_generate_specs:201`), `domains/agents/services/seed_agent_bases.py` (add `revise-spec` base), `api/v1/areas.py` (endpoint); Test `back_end/tests/test_revise_spec.py`.

**Interfaces:**
- Produces: `sweep.revise_area_spec(*, repository_uid, area_key, instruction, triggered_by="") -> ReviseSpecResult(run_uid, errors, summary)` — composes intent via `compose_agent_intent(agent_key="revise-spec", structural=_GENERATE_SPECS_TOOLING_CONTRACT, existing_state_listing=<area key/title/spec/scope + instruction>)` and `trigger_run(playbook="ask", …)`, producing one `AreaEdit`. `POST /areas/{uid}/revise-spec {instruction}` → `{run_uid}`.

- [ ] **Step 1:** Test that `revise_area_spec` with a missing area raises `LifecycleError`, and with a real area dispatches one run (monkeypatch `trigger_run`) whose intent contains the instruction + current spec.
- [ ] **Step 2: Run** → FAIL. **Step 3:** Implement mirroring `run_generate_specs` (single target + instruction slot); add the seeded `revise-spec` agent base (reuse the "what a good spec contains" body + a line to honour the maintainer instruction). Add the API route calling it.
- [ ] **Step 4: Run** `pytest back_end/tests/test_revise_spec.py` → PASS. **Step 5: Commit** `feat(specs): one-shot AI revise → AreaEdit`.

### Task 9: Chat widget can edit the viewed spec

**Files:** Modify `domains/runs/services/chat_context.py` (snapshot an `area` subject), `front_end/src/composables/usePageContext.ts:14` (add `area-detail` → `{type:'area'}`), `front_end/src/types/api.ts` (extend `CommentSubjectType` with `'area'` if needed); Test `back_end/tests/test_chat_context.py` (extend).

**Interfaces:**
- Consumes: `propose_area_edit` (already in `PLATFORM_WRITE_TOOLS`, already available to chat runs — verified). Consumes area subject `{subject_type:"area", subject_uid}`.
- Produces: chat preamble includes the area's key + current spec when started from an area page, so "tighten criterion 3" lands an `AreaEdit`.

- [ ] **Step 1:** Test `build_chat_preamble({"subject_type":"area","subject_uid":<uid>})` includes the area key + spec excerpt.
- [ ] **Step 2: Run** → FAIL. **Step 3:** Extend the chat-context subject snapshot switch to load `Area` and render key/title/spec; add `'area'` to the frontend `DETAIL_ROUTES` + `CommentSubjectType`.
- [ ] **Step 4: Run** `pytest back_end/tests/test_chat_context.py` → PASS. **Step 5: Commit** `feat(specs): global chat can revise the viewed area spec`.

---

## Stage 4 — UI/UX

### Task 10: Shared tree helper + hierarchical Features

**Files:** Create `front_end/src/lib/treeRows.ts`; Modify `AreasView.vue` (Features tab `:625` + refactor `partitionRows:180` onto it), `AreaDetailView.vue` (sub-features `:315`); Test `front_end/src/lib/__tests__/treeRows.spec.ts`.

**Interfaces:**
- Produces: `buildTreeRows<T>(items: T[], keyOf: (t:T)=>string): TreeRow<T>[]` where `TreeRow = {type:'group', key, name, depth} | {type:'leaf', key, name, depth, item:T}`. Extracts the `FolderNode` walk from `DocumentationView.vue:173-217` (split key by `/`, synthesize group rows for missing intermediate prefixes, depth = segment index, sorted).

- [ ] **Step 1:** Unit test: features `["a/x","a/y","b/z"]` → group `a`(depth0), leaves `a/x`,`a/y`(depth1), group `b`, leaf `b/z`.
- [ ] **Step 2: Run** `npm --prefix front_end run test -- treeRows` → FAIL. **Step 3:** Implement `treeRows.ts`. **Step 4:** Render Features tab via `buildTreeRows(features, a=>a.key)` with the same group/leaf row markup the Subsystems tab uses; do the same for `AreaDetailView` sub-features. Optionally refactor `partitionRows` to call it (keep `fileTotal` rollup).
- [ ] **Step 5: Run** the spec → PASS; visually confirm nesting. **Step 6: Commit** `feat(areas): shared tree helper + hierarchical Features`.

### Task 11: Campaign detail — clickable/collapsible parts + plan stat-header + batch

**Files:** Modify `front_end/src/views/CampaignDetailView.vue` (parts table `:410`, plan panel `:372`); no test framework change (component-level manual QA).

**Interfaces:** Consumes `plan_summary.total_runs`/`by_kind` (Task 5). Consumes `campaign.kind`/`child_uids`.

- [ ] **Step 1:** Parts table → collapsible rows: collapsed = `idx · kind badge · truncated title (truncate class) · file_count · "N lenses" chip · state · Run link`; a chevron expands to full `scope_paths` + lens list. Run cell = `RouterLink` to run-detail when `p.run_uid`, else expand-only.
- [ ] **Step 2:** Add a stat-header above the table: big **`total_runs`** + by-kind counts + covered/uncovered files bar + oversized/degraded chips (fall back gracefully when `plan_summary.total_runs` absent for legacy campaigns — derive from `parts.length`). Move `planLines` (`:235`) into a collapsible "How this plan was built" under the stats.
- [ ] **Step 3:** When `campaign.kind === 'batch'`, render a children roll-up (three `RouterLink`s to child campaigns + their status/counts from `summary`) instead of a parts table.
- [ ] **Step 4:** Manual QA against a live campaign (see Verification). **Step 5: Commit** `feat(campaigns): readable clickable parts + plan stat-header + batch view`.

### Task 12: New campaign dialog — kind/coverage/selection + live preview

**Files:** Modify `front_end/src/components/campaigns/NewCampaignDialog.vue`, `front_end/src/stores/campaignStore.ts` (add `previewPlan`), `areaStore.ts` (add `reviseSpec`).

**Interfaces:** Consumes `POST /repositories/{uid}/campaign-plan-preview` (Task 7). Replaces the `scope==='local'` lens filter with `default_lens_keys(kind)` semantics (fetch all lenses; pre-check the kind's defaults).

- [ ] **Step 1:** Add `kind` selector (Subsystem / Feature / Global / Audit-everything), a **multi-select** coverage picker over the kind's tree (reuse `buildTreeRows` with checkboxes; hidden for global/batch), a `selection` control (All / Stale / Rotation-k), keep effort. Lens list pre-checks `default_lens_keys(kind)`.
- [ ] **Step 2:** `campaignStore.previewPlan(repoUid, body)` → the preview endpoint; show a live line `≈ {total_runs} runs · {areas} areas · {files} files · {uncovered} uncovered` + by-kind, updating on every dial change (debounced, mirror existing `areaPrefix` watcher `:98`). Replace the weak explanation text with this.
- [ ] **Step 3:** Submit sends `{kind, coverage_keys, selection, lens_keys, effort, max_parallel, title}`.
- [ ] **Step 4:** Add `areaStore.reviseSpec(uid, instruction)` (→ Task 8 endpoint) + a spec editor / "Revise with AI" affordance on `AreaDetailView` (edit → `patchArea`; instruct → `reviseSpec`).
- [ ] **Step 5:** Manual QA. **Step 6: Commit** `feat(campaigns): kind/coverage/selection dialog + live plan preview + spec editing`.

---

## Verification (end-to-end)

1. **Backend unit:** `cd back_end && pytest tests/ -q` — all green; specifically `test_campaign_planner.py`, `test_bundle_siblings.py`, `test_campaign_batch.py`, `test_lens_defaults.py`, `test_revise_spec.py`, `test_chat_context.py`.
2. **Migrations:** bring the stack up (`docker compose up -d`), run the migrate script (`back_end/scripts`), confirm `m0013`/`m0014` apply and a pre-existing campaign still opens.
3. **Live smoke (QA user, per CLAUDE.md dev-auth):**
   - Areas → Features tab shows a **nested tree**; area detail shows sub-features nested.
   - New campaign → pick **Feature** kind → multi-select two feature groups → preview shows `≈ N runs`; launch → parts are feature spec-audits; a pending part row expands, a dispatched part links to its run.
   - New campaign → **Audit everything** → one parent with three child links; children run; parent rolls up.
   - Open an area → edit spec inline (saves); "Revise with AI" → an `AreaEdit` appears in the review queue; from the chat bubble on the area page, "make criterion 2 concrete" → another `AreaEdit`.
   - Global campaign → parts are one whole-repo sweep per global lens.
4. **Regression:** an old (`template`-only) campaign detail page still renders (stat-header falls back to `parts.length`).

## Notes / risks
- `CampaignDetailView.vue` (619 lines) and `DocumentationView.vue` (1395) are large; keep new logic in `treeRows.ts` + small computed helpers rather than growing them further.
- Global parts already dispatch after non-global parts terminate (`tick.py:75`); in single-axis campaigns this is a no-op, safe.
- The plan-preview endpoint MUST share the exact `_plan_parts` pure path so the previewed `total_runs` cannot drift from the launched plan.
