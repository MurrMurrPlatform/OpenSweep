# Campaigns / Lenses / Specs redesign + Areas UI — design

Date: 2026-07-23
Branch: `fix/freshness-features-quality-pass`
Status: approved in brainstorm, pending written-spec review

## Context

OpenSweep partitions a repository into an **Area map** (subsystem leaves that
tile the tree exclusively + feature overlays that cut across it + ignore
areas). **Campaigns** turn that map into a list of audit **parts**, each
dispatched as one **Run**. The current design has accumulated three coupled
problems this spec addresses in one pass (one thread, one PR):

1. **The lens model welds execution mode onto lens identity.**
   `lens.scope = local | global` means `architecture-review` and
   `implementation-gaps` can *only* run whole-repo, and the 8 code-quality
   lenses can *only* run per-area. There is no way to run `architecture-review`
   on a single subsystem, or to add lenses to a feature audit.

2. **Campaigns are rigid templates (`full` / `rotation` / `focused`).** Each
   template hard-codes a partition selection + a lens selection + a globals
   policy. There is no "features only", no "subsystems only", no
   multi-select coverage, and `full` audits subsystems **and** features in one
   launch (double-auditing shared files) — which the product owner has decided
   is not a workflow they want.

3. **Feature specs are second-class.** A `generate-specs` agent drafts them,
   but they land as pending `AreaEdit`s, are audited even when empty, and have
   no first-class editing surface. Auto-generated-from-code specs have a low
   value ceiling; the fix is to treat the auto-spec as a *seed* and make specs
   **editable** — by hand and via AI — so human/product intent can enter.

Plus a set of concrete Areas/Campaign **UI/UX** defects (Part C).

## Goals / non-goals

**Goals**
- Decouple lens execution mode from lens identity.
- Replace campaign templates with a small orthogonal dial set; campaigns become
  **single-axis** (subsystem *or* feature *or* global), never mixed.
- Add an **"audit everything"** batch that fans out into the three kinds.
- Make specs first-class editable objects (manual + AI-assisted via the
  existing global chat widget + a one-shot "revise" action).
- Fix the Areas/Campaign UI: readable/clickable part rows, a visual plan
  summary with a total run count, a live create-modal preview, and a
  hierarchical Features view.

**Non-goals (YAGNI, explicitly out of scope)**
- User-saved named campaign presets (ship built-in per-kind defaults only).
- A dedicated per-spec conversational thread surface (reuse the global chat
  widget instead).
- Hand-tunable granularity — bundling target sizes stay automatic.
- Re-running historical campaigns under the new model (they render read-only).

---

## Part A — Campaign / lens model

### The model

A campaign is:

> **kind** → **coverage** → **selection** → **depth** → **lenses**
> (granularity is automatic; execution **mode** is implied by kind)

- **kind** — `subsystem` | `feature` | `global` | `batch`. Top-level choice;
  determines which tree is sliced, the default lenses, and the execution mode.
- **coverage** — for `subsystem`/`feature`: a **multi-selected set of node keys**
  from that kind's tree (empty = the whole tree). `global` has no coverage tree;
  `batch` has none (its children do).
- **selection** — `all` | `stale` | `rotation` (k least-recently-covered).
  Applies to subsystem/feature; global selects nothing (one sweep per lens).
- **depth** — a per-campaign effort tier (`normal` | `deep`), today's semantics.
- **lenses** — the lens keys this campaign runs; pre-filled from a per-kind
  default map, fully overridable.

**Execution mode is derived from kind**, not chosen per lens:
- `subsystem` / `feature` campaigns → lenses run **per-area** (rendered into
  each part's run checklist), exactly like today's area/feature parts.
- `global` campaign → each selected lens runs **whole-repo** (one sweep part
  using the lens's `global_agent_key`), like today's global parts.

This satisfies "architecture-review as a normal lens on subsystems": the *same*
lens is a per-area check in a subsystem campaign and a whole-repo sweep in a
global campaign — the campaign it lives in picks the mode.

### Lens model changes

- **Drop `lens.scope`** (`domains/lenses/models.py`, `seed_lenses.py`,
  `schemas.py`). All lenses become plain checks.
- **Keep `lens.global_agent_key`** — it's how a lens runs in whole-repo mode
  (global campaigns). Lenses without one cannot be used in a global campaign.
- **Per-kind default lens map** lives in code (a seed/registry constant), not on
  each lens:
  - `subsystem` → the 8 code-quality lenses (`bugs`, `security`, `performance`,
    `error-handling`, `legacy-patterns`, `refactor-opportunities`,
    `simplification`, `test-gaps`).
  - `feature` → `implementation-gaps` (default), others opt-in.
  - `global` → the lenses that have a `global_agent_key`
    (`architecture-review`, `implementation-gaps`).
- Existing lens rows keep their `global_agent_key`; the `scope` column is
  removed via a Neo4j migration (`back_end/migrations/`).

### Planner changes (`domains/campaigns/services/planner.py`)

`build_plan(template, ...)` is replaced by a kind-dispatched builder. The pure
partition helpers are reused:

- **`normalize_areas`, `areas_from_map`, `bundle_siblings`** — unchanged.
- **`filter_by_prefix` → generalize to `filter_by_keys(areas, keys)`** — accept
  a **set** of node keys (multi-select coverage); an area is included if its key
  equals or nests under any selected key. Empty set = everything. Reused for
  both subsystem and feature coverage.
- **New `build_plan(kind, areas|feature_areas, lenses, *, selection, k,
  path_recency)`**:
  - `subsystem`: bundled subsystem areas filtered by coverage; apply `selection`
    (`all` = every area; `stale` = stale areas only; `rotation` = k
    least-recently-covered via existing `_area_recency`); one part per area with
    the campaign's lens set.
  - `feature`: feature leaves filtered by coverage; apply `selection`
    (`all`/`stale`; rotation falls back to stale for features); one part per
    leaf with the campaign's lens set (default `implementation-gaps`).
  - `global`: one whole-repo part per selected lens (must have
    `global_agent_key`).
  - `batch`: produces **no parts**; handled at the service layer (fan-out).

`selection` subsumes today's `full` (=`all`) vs `rotation` (=`rotation`)
distinction; `stale` is new and is the natural nightly default.

### Batch campaigns (`domains/campaigns/services/campaign_service.py`)

- `Campaign.kind = "batch"` is a **parent**: on launch it creates three child
  campaigns (`subsystem`, `feature`, `global`) with the batch's shared inputs
  (depth, selection where applicable) and default per-kind lenses, then tracks
  them.
- Data: children carry `parent_uid`; the batch carries `child_uids`. The batch's
  view rolls up child digests (reuse `finalize.build_summary` per child +
  a parent aggregation). Children are ordinary campaigns openable on their own.
- Legal-status handling mirrors the existing matrix; the batch reaches `done`
  when all children are terminal.

### Campaign model changes (`domains/campaigns/models.py`)

Add:
- `kind` (`subsystem|feature|global|batch`), indexed.
- `coverage_keys` (JSON list; empty = whole tree).
- `selection` (`all|stale|rotation`).
- `parent_uid` / `child_uids` for batch wiring.

Keep `k`, `effort`, `lens_keys`, `parts`, `plan_summary`, `max_parallel`.
`template` is retained read-only for historical rendering.

`plan_summary` extends with an explicit **run-count breakdown** (already carries
`area_parts`/`feature_parts`/`global_parts`/`bundled_leaves`/`oversized`/
`degraded`); add `total_runs` and a per-kind split for the header UI.

### Migration / back-compat

- Neo4j migration adds the new Campaign fields and drops `lens.scope`.
- On read, historical campaigns map `template → kind`:
  `full → batch` (render as a batch view over its single legacy part list —
  read-only), `rotation → subsystem` + `selection=rotation`,
  `focused → subsystem` + single-lens. `area_prefix` (single) backfills
  `coverage_keys = [area_prefix]` when set.
- No historical campaign is re-planned or re-run automatically.

---

## Part B — Specs as editable objects

The auto-spec is a **seed**; intent enters via editing.

### Manual editing
- `area_service.update_area` already accepts `spec`. Add a spec **editor** on
  the area detail page (markdown edit → save, or → propose `AreaEdit` if we keep
  the human-owned/propose discipline; **decision: direct save for the area's
  human owner**, since areas are human-owned and agents are the ones who
  propose).

### One-shot AI "revise" (instruct → diff)
- New action on the area page: user types an instruction ("make criteria
  checkable", "add the webhook-retry idempotency guarantee"). Dispatches a run
  that reuses the `generate-specs` agent base + `_GENERATE_SPECS_TOOLING_CONTRACT`
  scoped to **one** area, with the instruction in an added slot, producing a
  single `AreaEdit` (`proposed_spec`) rendered as a diff to accept/reject.
- Backend: a thin `revise_area_spec(area_uid, instruction)` in
  `domains/runs/services/sweep.py` (sibling of `run_generate_specs`).

### Conversational AI editing (global chat widget)
- Reuse the existing **`OpenSweepChatWidget.vue`** (bottom-right; a
  `surface=chat` run with `usePageContext`). No new chat surface.
- Wire-up:
  1. Extend `usePageContext` so an area/spec page publishes the current
     `area_uid`/`area_key` into chat context.
  2. Ensure the chat agent's toolset includes `propose_area_edit` (platform
     tool) so "tighten criterion 3" from the bubble lands an `AreaEdit`.
  3. Surface resulting `AreaEdit`s in the existing review queue — one accept/
     reject path for **all** spec changes (manual, one-shot, chat).

### Audit-time behavior (unchanged, already correct)
- Specless feature parts already degrade to a plain sweep + `feature.spec_missing`
  notification (`part_dispatch.py:144-192`). Keep. With editing, the fix path is
  now: edit the spec (any surface) → re-plan.

---

## Part C — Areas / Campaign UI/UX

### C1. Part rows — readable + clickable (`CampaignDetailView.vue`)
- Make each part row **collapsible**. Collapsed = one clean line:
  `idx · kind badge · truncated title · file count · "N lenses" chip · state ·
  run link`. Expanded = full `scope_paths` + full lens list.
- The **Run** cell links to the child run once dispatched; a `pending` row only
  expands (no run yet). This fixes both the drill-in (your #2) and the
  title-overflow in one move.

### C2. Plan summary — visual, with total runs (`CampaignDetailView.vue`)
- Lead with a **stat header**: prominent **total run count** + by-kind
  breakdown (N subsystem / N feature / N global), a covered-vs-uncovered files
  bar, and chips for oversized / degraded. Source: extended `plan_summary`.
- The current "How this plan was built" prose becomes a **collapsible details**
  panel beneath the stats.

### C3. Create-modal live preview (`NewCampaignDialog.vue`)
- As kind / coverage / selection / lenses change, show a **live preview**:
  `≈ N runs · M areas · X files · Y uncovered`, with the same by-kind
  breakdown, **before** launch. Reuse the `preview_areas` endpoint + a dry
  part-count (run the pure planner without persisting). Also replace the
  current weak explanation text with this preview.

### C4. Feature hierarchy (`AreasView.vue` Features tab + `AreaDetailView.vue`)
- Features currently render **flat** despite nested keys
  (`slack-integration/inbound-bot` under `slack-integration`).
- **Extract one shared tree helper** from the two existing implementations that
  already work — the Docs `FolderNode` walk (`DocumentationView.vue:129-212`)
  and the Subsystems `partitionRows` (`AreasView.vue`) — into a reusable
  composable/component (`buildTreeRows(items, keyOf)` → `{type: 'group'|'leaf',
  name, depth, ...}` with synthetic group rows for missing intermediate
  prefixes). Apply it to Features on both the Features tab and the area detail
  page, with parent charters as group headers and leaves nested.

---

## Data flow (end to end)

1. **Map** (unchanged): `map-areas` proposes Areas; humans accept.
2. **Specs**: `generate-specs` seeds feature specs; humans/AI edit them (Part B);
   all changes flow through `AreaEdit` accept (except the owner's direct save).
3. **Plan**: create modal → `_plan_parts` → kind-dispatched `build_plan` over
   coverage-filtered, bundled areas/features → parts (+ live preview via the
   same pure path). Batch → three child plans.
4. **Dispatch** (`part_dispatch.py`): per-area parts render the lens checklist +
   (feature) spec; global parts dispatch the whole-repo agent per lens.
5. **UI**: detail page renders the stat header + collapsible/clickable parts;
   batch page rolls up children.

## Testing

- **Planner is pure → unit tests** (`back_end/tests/test_campaign_planner.py`,
  `test_bundle_siblings.py`): `build_plan` per kind; multi-select coverage via
  `filter_by_keys`; each `selection` strategy; global one-part-per-lens; batch
  fan-out produces the three child input sets.
- **part_dispatch**: mode selection by kind; specless-feature degradation
  preserved.
- **Migration**: `template → kind` read mapping; `area_prefix → coverage_keys`.
- **Frontend**: tree-helper unit test (synthetic group rows); create-modal
  preview count matches the planned part count; part-row collapse/expand +
  run-link states.

## Open implementation risks

- Removing `lens.scope` touches seeding + any UI that filters by scope
  (`NewCampaignDialog` offered locals only) — audit all readers.
- Batch status/finalize aggregation is the most novel piece; keep the parent
  thin (it owns no parts, only child_uids + a roll-up).
- The create-modal preview must run the planner without persistence — ensure
  `preview_areas`/dry-count share the exact pure path the real plan uses so the
  number can't drift.
