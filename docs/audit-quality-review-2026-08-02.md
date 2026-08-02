# Can OpenSweep accurately audit a repo? — capability review

**Date:** 2026-08-02 · **Scope:** `opensweep` @ `main` (efe58fb), post PR #48/#49
**Method:** five parallel code reads of the campaign/lens/agent/coverage/findings
domains, every claim below traced to source and independently re-verified.

Companion to `docs/audit-gaps.md` (which audits *this repo*). This document
audits **the auditing machine itself**.

---

## Verdict

**The orchestration is genuinely strong. The evidence chain is not.**

OpenSweep can point a capable agent at a well-defined slice of a repository with
good instructions, real static-analysis candidates, and a symbol graph — that
part works and is better than most tools in the category. What it cannot
currently do is **prove what it looked at, verify what it reported, or
synthesise the result into an analysis.**

Three independent things break the chain end to end:

1. **Load-bearing wires are cut.** Six verified defects mean features that exist
   in the UI and in the prompts silently do nothing (§2). One of them makes a
   user-selectable campaign mode a guaranteed no-op.
2. **Nothing verifies anything.** Coverage is model self-report with a fallback
   that relabels the dispatch order as a result. Findings from audit runs get no
   skeptic pass — the skeptic exists, but only on the PR path, is off by
   default, and its verdict is wired to nothing (§3).
3. **There is no synthesis stage.** A campaign's output is a count digest, not an
   analysis. The one run type that produces a real Analysis (deep-scan) is
   manual-only, unschedulable, and disconnected from campaigns and coverage (§4).

`docs/audit-gaps.md` already reached the same conclusion from the other
direction, in its own words on finding S19:

> *"this was a false-positive HIGH that survived static reasoning and died on
> first execution — exactly the verification pass OpenSweep isn't running."*

**On "do we need more global runs":** not primarily. Two global lenses plus
deep-scan is a reasonable starting catalogue. The problem is that whole-repo runs
today are the *least* accountable runs in the system, and the cross-cutting
channel that feeds them is broken in the flagship path. Fix accountability and
plumbing first (§6 P0/P1); add 2–3 targeted global lenses after (§6 P2).

---

## 1. What exists today

### Run taxonomy

| Kind | Scope | Trigger | Coverage contract? |
|---|---|---|---|
| Campaign part `area` | one Area leaf / sibling bundle, explicit path list | campaign tick | ✅ full (`_REPORTING_CONTRACT`) |
| Campaign part `feature` | one feature leaf + its spec inlined | campaign tick | ✅ full |
| Campaign part `global` | **whole repo**, one part per global lens | campaign tick, gated after areas | ❌ none |
| Campaign `batch` | fans into 3 child campaigns | user / cron | ❌ (roll-up only) |
| `run_audit` doc-scoped | one Doc page's `watch_paths` | user / auto-audit | partial |
| `run_audit` area-scoped | union of selected Areas | user | partial |
| `run_audit` whole-repo | whole repo | user | ❌ none |
| `run_deep_scan` | whole repo → **Analysis** node | user only | ❌ (prose only) |
| Scheduled `deep-issue-hunt` / `security-audit` | **whole repo**, one run | cron (seeded disabled) | ❌ none |
| PR `review` | `git diff base...head` | push webhook | n/a (verdict) |
| PR re-review | `git diff prior_verdict_sha...head` | push webhook | n/a |
| PR `verify` (skeptic) | the verdict's findings | after review, **if opted in** | n/a |

### Catalogue
- **10 lenses** (`seed_lenses.py`): 8 local, 2 global (`architecture-review`,
  `implementation-gaps`).
- **16 agent bases**, **14 library variants**, **7 workflow-stage defaults**.
- **4 effort tiers** — deep = 4 h wall, 3000 tool turns, 8 continuation passes.
- Default subsystem campaign gives **one run all 8 local lenses** over ≤150 files.

### What is genuinely good

Worth stating plainly, because the rest of this document is critical:

- **The partition math is excellent.** `planner.normalize_areas` /
  `areas_from_map` enforce "every file owned by exactly one leaf", compute
  overlap/dead-scope/oversized health, and `bundle_siblings` keeps run count
  proportional to work rather than to map granularity (`planner.py`).
- **Deterministic pre-pass exists and is real.** ruff/vulture/deptry/semgrep/knip
  run in the sandbox before the agent, capped at 40 candidates, framed correctly
  as *"Tools find candidates, the agent investigates"* (`static_analysis.py:1-11`).
- **A symbol/call graph is available per workspace** over MCP
  (`infrastructure/code_graph.py`) — `trace_path`, `search_graph`,
  `get_architecture`.
- **The prompt library is disciplined.** Every lens ends with *"Checked, nothing
  found is a valid verdict"*; security has explicit do-NOT-file exclusions;
  precision/recall is an explicit per-variant dial.
- **The review agent isn't trusted to count its own blockers** — blocking count is
  derived server-side from the MergePolicy, divergence audited
  (`pull_request_service.py:618-679`). This is exactly the right instinct, applied
  in exactly one place.
- **Trust scoring is well-designed** — corroboration, tool confirmation, human
  outcome feedback, with `dismissed` weighted below every model signal
  (`findings/services/trust.py`).
- **Rotation's refusal to rank on `coverage_source`** is correct and the reasoning
  in `coverage_recency_for` is sound (see [[doc-run-health-loop-pr45]]).

---

## 2. Verified defects

All six confirmed by reading source; three are user-facing.

### B1 — `selection="stale"` subsystem campaigns always plan zero parts 🔴

`_area_map_inputs._leaf_dict` sets `"stale": area_is_stale(a)`
(`campaign_service.py:212`). `planner.areas_from_map` then rebuilds every area
through `_area()` (`planner.py:382-391`), which emits only
`{title, scope_paths, doc_uids, file_count}` + `area_key/oversized/dead_scope_paths`.
**The `stale` key is dropped.** `build_plan_by_kind` subsystem branch then filters
on `a.get("stale")` (`planner.py:630-632`) → always falsy → **zero parts**.

The campaign launches, ticks once, sees `pending_left == 0`, and finalises `done`
with an empty summary. No error, no warning.

It is user-reachable: `NewCampaignDialog.vue:342` offers
*"Stale — code changed since last review"*.

Feature campaigns are unaffected — `_count_features` uses `dict(f)`
(`campaign_service.py:289`), preserving `stale`.

**Why tests missed it:** `test_campaign_planner.py:250-259` fabricates area dicts
*with* a `stale` key, so `build_plan_by_kind` passes in isolation. The seam
between `areas_from_map` and `build_plan_by_kind` is untested.

### B2 — batch campaigns defeat the global-after-areas ordering gate 🔴

`tick.plan_tick:87` correctly holds global parts until every non-global part is
terminal, so escalation digests see the full campaign's findings. This is tested
(`test_campaign_tick.py:85-110`).

**The batch path bypasses it.** `batch.create_batch` fans out into three
*separate* campaigns (`_CHILD_KINDS = ("subsystem","feature","global")`,
`batch.py:23`) and `launch_batch` launches all three immediately
(`batch.py:113-136`). The global child contains only global parts, so
`areas_terminal = all(... for p in parts if kind != "global")` is `all([])` →
`True` → dispatches at once, in parallel with the subsystem child.

Nothing gates on `parent_uid` — grep confirms it is only ever written, never read
for sequencing.

**Effect:** the flagship "audit everything" run sends its two whole-repo sweeps
out with an escalation digest that is empty on a first batch, and one cycle stale
thereafter. The cross-cutting channel is disconnected precisely where it matters
most.

### B3 — the findings look-before-write contract points at tool names that don't exist 🟠

`OPENSWEEP_FRAMING_HEADER` and `LOOK_BEFORE_WRITE_FOOTER` instruct the agent, as
a **mandatory** step before every `create_finding`, to call
`opensweep_list_findings` / `opensweep_search_findings` / `opensweep_get_*`
(`_intent_helpers.py:42, 59-60`).

The real tools are `opensweep_platform_read_list_findings`,
`_read_search_findings`, `_read_get_finding` (`mcp_app.py:151-159`). And:

- `PLATFORM_READ_TOOLS = ("list_docs", "read_doc", "search_memory")`
  (`prompt_kit.py:56`) — findings reads are **not** in the generated tool list.
- `_MCP_NAMING_NOTE` (`prompt_kit.py:212-218`) enumerates exactly which read tools
  carry the `read` infix — `list_news_items`, `list_interests`, `list_docs`,
  `search_memory`, `read_doc` — and **omits findings**.

So the one mandatory step that would let an audit run see prior findings names
three tools that don't resolve, and the real ones are never advertised. Nothing
injects findings into the briefing either (`build_briefing` has no findings
section). **Audit runs are effectively blind to prior findings.**

### B4 — `scope_hint` is never populated 🟡

`part_dispatch.py:226` reads `part.get("scope_hint")` to steer an
`area_prefix`-scoped global sweep. Grep confirms `scope_hint` appears **only** on
lines 225-226 — the planner never writes it. The steer always degrades to the bare
prefix string.

### B5 — the skeptic verdict never reaches the trust score 🟠

`trust._VERIFICATION_DELTA` (`confirmed +0.20`, `refuted −0.50`) is documented as
*"the strongest signal either way"* (`trust.py:36-37`). The only production caller
is `finding_service.py:104`: `trust=trust_score_for(f)` — no `verification_status`
argument, defaulting to `""` (`trust.py:116`). `verification_status` lives on the
PR `Verdict`, not on the `Finding`, and nothing joins them. **The delta is dead
code.** `test_finding_trust.py` tests the function directly, so the dead wiring is
invisible to the suite.

### B6 — escalation tags are never consumed, and the digest silently truncates 🟡

`_escalation_digest` selects open findings tagged `escalate:<lens>`, sorts newest
first, takes 20 (`part_dispatch.py:77-91`). Nothing ever clears the tag or marks
it consumed — grep finds no `escalate` handling anywhere in `domains/findings/`.
So an escalation re-enters every future sweep's digest until a human closes it,
while items 21+ drop off with no signal. On a busy repo the queue becomes
newest-20-forever.

### Minor: `Lens.wants` is dead configuration

`wants: ["static_analysis"]` on bugs/security/simplification/architecture-review
is stored, checksummed, and exposed in the DTO, but never read. Static analysis
gates on `run.playbook in {"review","ask"}` (`lifecycle.py:674`) instead.

---

## 3. The accountability gap

### Coverage is self-reported, and the fallback lies

`checked_service._coverage_fields:65-69`:

```python
source = "reported"
covered = _paths(coverage.get("covered_paths"))
if not covered:
    covered = _paths(target.get("paths"))   # the dispatch scope
    source = "inferred"
```

`build_coverage` (`complete_run.py:62-95`) only coerces to non-empty strings. No
path is checked against the file tree, against the run's own scope, or against
what the agent actually read. The run's tool-call event stream exists
(`append_event`) and **nothing derives coverage from it**.

The code is candid about this — *"a run that read one page of a 35-page scope
still lands the whole scope in covered_paths… that fallback is a guess, not
evidence"* (`checked_service.py:45-49`). But the honesty lives in a docstring and
one UI badge, not in any gate.

Two further leaks:
- `record_for_run` stamps **every Doc whose `watch_paths` match any finding's
  `affected_paths`** (`checked_service.py:94-106`) — a doc the run never opened
  gets a "checked" stamp.
- Whole-repo runs (global parts, deep-scan, `deep-issue-hunt`, `security-audit`)
  carry **no coverage contract at all** — `_REPORTING_CONTRACT` is attached only
  by `_dispatch_area`. There is no way to know what fraction of the repo a
  "whole-repo" run looked at.

### Coverage never expires

`freshness.py` is one clean axis (review recency vs code change), and
`Checked` is deliberately a separate axis that does not interact
(`area_freshness.py:8-16`). The consequence is not stated anywhere: **a code
change never invalidates an audit stamp.** `Checked.revision` records the sha but
no reader compares it to HEAD. Once a path lands in any non-failed stamp's
`covered_paths`, it counts as covered forever for rotation.

Combined with B1 (`stale` selection broken), there is currently **no working
"re-audit what changed" mode** — only `rotation`, which is change-blind.

### Audit findings get no verification

The skeptic pass is well-built (`verification_run_service.py`) — fail-closed,
prompted to refute, silence treated as confirmation. But:
- it requires `review_run.linked_pr_uid` (`playbooks.py:122-124`) → **PR path
  only**;
- it defaults to **off** (`workflow.py:64`);
- its verdict doesn't reach trust (B5).

Meanwhile the highest-volume audit configurations are the most recall-tuned.
`deep-issue-hunt` (deep effort, seeded for daily+weekly cron) says:

> *"uncertain-but-serious beats certain-but-trivial: file plausible high-impact
> issues you could not fully confirm."*

That is a defensible stance **only** with a verification stage downstream. There
isn't one.

### The false-positive loop is open

Dismissing a finding applies `−0.60` trust to *that finding*
(`trust.py:60`) and, incidentally, suppresses byte-similar re-reports via the
layer-1 `dedupe_key` lookup, which has no status filter
(`create_finding.py:149`). But:
- there is no suppression list and no known-FP injection into any prompt;
- `read_list_findings` defaults to `status="open"`, so dismissed findings are
  invisible to an agent even if it could call the tool (B3);
- a rephrased title or a moved file produces a fresh open finding at full trust,
  because layer-2 similarity filters `status__in=["open","ticketed"]`.

Dedup itself is string/hash-based (sha1 of repo + digit-stripped title +
**basename**), with a `difflib` 0.75 fallback. Digit-stripping is intentional
("line 42" ≡ "line 99") but merges `index 3` with `index 7`; basename-only merges
`src/auth/session.py` with `vendor/legacy/session.py`.

---

## 4. The synthesis gap

`finalize.build_summary` produces `counts.by_severity`, `counts.by_part`,
`coverage.parts`, `coverage.holes`, `feature_rollup`, `failed_parts`. That is a
**tally, not an analysis**. Notably, `lens_verdicts` — the per-lens
checked-clean/checked-findings/skipped signal the whole contract exists to
produce — is written to `Checked` and then **never read by the campaign digest**.

No agent ever reads the campaign's findings as a set and writes "here is what is
wrong with this repository and what to do first."

The one run type that does produce that — `run_deep_scan` → `Analysis` (health
grade, 12-dimension scorecard, narrative sections, coverage notes, strengths,
questions) — is:
- **manual only** (`POST /sweep/deep-scan`, no seeded cron binding);
- **not a lens and not a campaign kind** — it cannot be composed into the audit
  loop;
- **disconnected from coverage** — its coverage section is agent-authored prose;
- **superseded by nothing** — a second deep-scan chains via `supersedes`, but
  campaign findings never update it.

So the product has an excellent analysis artifact and no automated path to it.

### Cross-cutting analyses that have no home

Area runs are contractually forbidden from looking outside scope
(`_scope_contract`: *"Do not investigate outside this scope"*;
`_ESCALATE_INSTRUCTION`: *"do NOT investigate it"*). The only escape hatches are
the two global lenses. These have **no lens, no agent, and no tag to escalate to**:

| Analysis | Status |
|---|---|
| Architecture / boundaries / coupling | ✅ global lens |
| Promise-vs-reality / stubs / dead flags | ✅ global lens |
| Cross-area duplication | ❌ prose only in `simplification` |
| Cross-area dead code / unused exports | ⚠️ knip/vulture exist but **filtered to scope** |
| Unused dependencies | ⚠️ deptry exists but **filtered to scope** |
| Dependency cycles | ❌ (code graph could answer this; nothing queries it) |
| API contract drift backend↔frontend | ❌ |
| Migration / schema consistency | ❌ (23 migrations, no check) |
| Config drift | ❌ |
| Cross-service data flow | ❌ |
| End-to-end test coverage gaps | ⚠️ `test-gaps` is per-area only |

**The scope filter is actively counterproductive here.** `filter_candidates(...,
allowed_paths=target["paths"])` (`lifecycle.py:706`) strips exactly the signals
that are *inherently* whole-repo: knip's unused-file report, deptry's unused
dependencies, vulture's dead code. An area run cannot see them; the whole-repo
runs that could are the ones with no coverage accounting.

And the code graph — the one deterministic structural asset — is **agent-facing
MCP only**. Nothing in the backend queries it, aggregates it, or derives cycles,
coupling, or god-module metrics from it.

---

## 5. Cost model (for calibration)

A 400-file repo with 30 subsystem leaves, using `bundle_siblings` (min 50, max
150 files):

- **~7–20 area parts** (bundling depends on parent-key distribution) + 1+
  remainder parts
- **+1 part per feature leaf** (features are never bundled)
- **+2 global parts**
- `max_parallel` default 5, clamped by provider headroom

A `batch` ≈ (area parts) + (feature leaves) + 2 runs. Each area run is a single
`normal`-effort invocation (1 h wall, 200 tool turns) covering **all 8 lenses
sequentially** over up to 150 files. That is thin — 8 disciplines, ~25 turns each.

---

## 6. Recommendations

Ranked by (impact ÷ effort). P0 items are small and unblock things that are
already built.

### P0 — repair the cut wires

| # | Fix | Effort |
|---|---|---|
| R1 | **B1**: carry `stale` (and `has_spec`) through `areas_from_map` into the area dict, and add a seam test that plans a stale subsystem campaign from real `Area` rows. | S |
| R2 | **B2**: gate a batch's `global` child launch on its subsystem+feature siblings reaching terminal. Simplest: don't launch the global child in `launch_batch`; let the batch tick launch it once the other two are terminal. Add a test at the batch level, not just `plan_tick`. | S |
| R3 | **B3**: add `list_findings`/`search_findings`/`get_finding` to the dispatcher registry and `PLATFORM_READ_TOOLS`, extend `_MCP_NAMING_NOTE` to cover them, and fix the three tool names in `OPENSWEEP_FRAMING_HEADER` / `LOOK_BEFORE_WRITE_FOOTER`. Add a golden test asserting every tool name in a prompt resolves against `OPENSWEEP_PLATFORM_TOOL_OPERATIONS`. | S |
| R4 | **B5**: pass the finding's verification outcome into `trust_score_for`. Either denormalise the skeptic verdict onto `Finding` at `_dismiss_refuted`/confirm time, or join `FindingVerification` in the DTO. | S |
| R5 | **B4/B6**: populate `scope_hint`; add a `escalate-consumed:<lens>` tag (or `escalated_at`) written by the global sweep, and log when the digest truncates. | S |
| R6 | Delete `Lens.wants` or wire it — a config field nobody reads will be trusted by someone. | XS |

### P1 — make coverage and findings accountable

| # | Fix | Rationale |
|---|---|---|
| R7 | **Derive coverage from the tool-call event stream**, not from self-report. The run already emits tool events; extract read/grep file paths and store them as `covered_paths_observed` alongside the claim. Keep the claim (it carries intent), but surface the delta. This is the single highest-leverage change in this document — it converts every coverage number from an assertion into evidence. | Fixes the root of §3 |
| R8 | **Attach `_REPORTING_CONTRACT` to whole-repo runs too** (global parts, `run_audit` whole-repo, deep-scan, the seeded cron sweeps). A whole-repo run that reports `skipped_paths` honestly is far more useful than one that reports nothing. | Closes the biggest accountability hole |
| R9 | **Extend the skeptic pass to audit findings.** Reuse `verification_run_service` with a non-PR entry point: after a campaign finalises, dispatch one verify run over the campaign's new high-severity/low-confidence findings. Given `deep-issue-hunt`'s explicit "file unconfirmed" stance, this is the missing half of that design. | The report's central recommendation |
| R10 | **Roll `lens_verdicts` into the campaign digest.** "6 of 8 lenses checked-clean, `performance` skipped in 4 of 11 parts" is the honest coverage statement the contract already collects and then discards. | S, high signal |
| R11 | **Add a real "re-audit what changed" selection.** Rank areas by `code_changed_at > latest Checked.checked_at` for their scope (not by review staleness). This is what users mean by "stale"; B1's mode never did it even when it worked. | Restores a headline capability |
| R12 | **Feed dismissed findings back into prompts.** Inject the repo's recent `dismissed` finding titles into the audit briefing as a known-false-positive list. Cheap, and closes the loop the trust score already half-implements. | S |

### P2 — global reach, done properly

Only after P0/P1. More global lenses on top of broken plumbing multiplies noise.

| # | Add | Why this one |
|---|---|---|
| R13 | **Stop filtering whole-repo analyzer signals to area scope.** Run knip/deptry/vulture once per repo, store the report, and inject the *whole-repo* slice into global sweeps while keeping the scoped slice for area runs. | Recovers deterministic cross-area dead-code/dep signal for free |
| R14 | **Query the code graph server-side.** One job computing import cycles, coupling hotspots, and god-module rankings, fed into the `architecture-review` sweep as candidates. Turns the strongest structural asset from agent-optional into product-owned. | The graph is already indexed on every clone |
| R15 | New global lens: **`duplication-and-dead-code`** — the one cross-cutting analysis that is *structurally impossible* for a scoped run and that R13/R14 can feed with real candidates. | Highest-value gap |
| R16 | New global lens: **`contract-drift`** — backend API surface vs frontend client vs docs vs migrations. This repo alone has 23 migrations and a typed frontend client; drift here is silent and expensive. | Second-highest gap |
| R17 | **Make deep-scan schedulable and campaign-composable.** Seed a `deep-scan` cron binding (disabled), and let a `batch` campaign finish by dispatching one deep-scan-style **synthesis run** that reads the campaign's findings + lens verdicts + coverage and authors an `Analysis`. | Closes §4 — turns "audit" into "analysis" |
| R18 | Consider **splitting the 8-lens area run** into 2–3 grouped runs (correctness+security / performance+error-handling / cleanup lenses), or default subsystem campaigns to `deep` effort. 8 disciplines × 150 files in 200 tool turns is the quietest quality ceiling in the system. | Measure before committing |

### Suggested sequencing

1. **Sprint 1 — P0 (R1–R6).** All small, all mechanical, all currently making
   shipped features silently no-op. R1 and R2 are user-visible correctness bugs.
2. **Sprint 2 — R7, R8, R10.** Coverage becomes evidence. This is what makes
   every number on the Areas board and the coverage strip defensible.
3. **Sprint 3 — R9, R11, R12.** Precision loop closes: verify, re-audit on
   change, learn from dismissals.
4. **Then P2**, with R17 (synthesis) first — it is what turns the answer to
   *"can it make an analysis?"* from "only if a human clicks deep-scan" into
   "yes, every campaign."

---

## Appendix — answering the question directly

> **Can OpenSweep accurately audit a repo?**

It can audit a *slice* accurately: the scoping, prompts, static-analysis
candidates, and code graph are good, and a competent model in that harness will
find real defects. It cannot currently **demonstrate** that it audited what it
claims, and it does not check what it reports.

> **Can it make an analysis?**

Only via `deep-scan`, manually triggered. Campaigns — the automated path —
produce counts, not analysis. R17 closes this.

> **Do we need more global runs?**

Not first. You have two global lenses and a deep-scan; the deficiency is that
whole-repo runs are the least accountable runs in the system (no coverage
contract), the escalation channel that feeds them is broken in the batch path
(B2), and the whole-repo deterministic signals are stripped before they arrive
(R13). Fix those, then add `duplication-and-dead-code` and `contract-drift`
(R15/R16) — those are the two analyses a scoped run *cannot* do by construction.
