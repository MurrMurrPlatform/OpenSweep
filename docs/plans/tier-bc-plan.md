# Plan — Tier B + C: scale, resilience, and closing the loop

Branch: `feat/tier-bc-scale-loop` (worktree `opensweep-tier-bc`, off `main`).
Pass 1–3 + the agent-autonomy MCP feature are now committed to `main` (`d25af3b`),
and this branch is even with it — so this worktree **already includes all of that
work**. Build Tier B/C directly on top; no rebase or merge coordination needed.

## What this pass delivers

The higher-effort audit items grouped by theme:

- **B — dispatch safety + async loop**: a cross-process lock that unblocks three
  duplicate-work races (B6/B7/B8), and moving the run-event hot path off the
  event loop (P3).
- **C — product completeness + scale**: the in-app **merge button** (G2), a
  **second write-capable executor** (G1), and **multi-replica / deploy-safe**
  operation (P7).

Execution mode: **all phases in one autonomous pass, no human intervention.**
Land each phase in order with tests, keep the suite green, then continue to the
next. P7 is the largest and riskiest; it is implemented in safe, independently-
verifiable sub-parts (P7a–P7e) — any sub-part that cannot be completed + verified
autonomously is landed as far as is safe and its residual documented in the PR,
rather than shipping a broken scale change.

---

## Dependency order (why this sequence)

1. **Phase 0 — the lock primitive** must exist before anything else in B, and is
   a hard prerequisite for P7 (the moment you run 2+ processes, the current
   in-process `asyncio.Lock`s are void).
2. **Phase 1 (B races)** and **Phase 2 (P3)** touch the dispatch + run-event
   paths; do them together while that code is in hand.
3. **Phase 3 (G1)** and **Phase 4 (G2)** are independent features — any order.
4. **Phase 5 (P7)** depends on Phase 0 (lock) + a resumption story; do it last.

---

## Phase 0 — cross-process lock primitive

New: `back_end/infrastructure/dist_lock.py`.

- Redis-backed lock: `SET key token NX PX ttl_ms`; release only if the stored
  token matches (Lua compare-and-del) so a slow holder can't delete a lock it no
  longer owns.
- Async context manager: `async with dist_lock(key, ttl_seconds=...) as got: ...`
  — `got` is False if the lock is held (caller decides: skip vs 409).
- **Fallback**: when Redis is unavailable (dev without the flag / tests), fall
  back to the existing module-level `asyncio.Lock` per key so behavior is
  unchanged in single-process dev. Reuse `infrastructure/redis_client.py`.
- Tests: `test_dist_lock.py` — acquire/contend/release, token-safe release,
  fallback path. Mock Redis (fakeredis or a stub).

Acceptance: two concurrent `async with dist_lock("k")` — only one gets it; the
loser waits or is told no; a crashed holder's lock expires after ttl.

---

## Phase 1 — B: apply the lock, fix the three races

### B6 — dispatch atomicity across processes
`domains/delivery/services/run_dispatch.py`
- Replace the per-process `_DISPATCH_LOCKS: dict[str, asyncio.Lock]` with
  `dist_lock` keyed on the dispatch target (`pr_uid` / `ticket_uid`).
- The `blocking_run`/`active_runs_for` read + `trigger_run` write must both sit
  inside the lock so the backend and the worker can't both pass the "no active
  write run" check and double-dispatch a fix/implement run.
- Test: two concurrent `_dispatch` calls for the same target → exactly one
  proceeds (the other sees the blocking run / is skipped).

### B7 — quota resume lease
`domains/runs/tasks/resume_paused.py` + `lifecycle.py:924-1035`
- Before enqueuing (or at pickup), take a per-run lease (`dist_lock("resume:"+uid)`
  with a ttl covering the prep window, OR an atomic status flip
  `paused_quota → resuming` conditional on the current value) so the 10-min beat
  can't enqueue a second `resume_run` while the first is still cloning.
- Release/settle the lease when the run reaches RUNNING or fails.
- Test: two `_scan_and_enqueue` ticks over the same paused run → one resume.

### B8 — execute_queued_run idempotency under acks_late
`domains/runs/services/lifecycle.py:443-493, 558-587`
- Flip the row `QUEUED → RUNNING` **conditionally on QUEUED** (a compare-and-set,
  under `dist_lock("run:"+uid)`) BEFORE the sandbox clone, not after. A
  redelivered `dispatch_run` task then finds status != QUEUED and returns early
  instead of cloning + dispatching a second agent.
- Test: two `execute_queued_run` for the same QUEUED run → second is a no-op.

---

## Phase 2 — P3: async run-event path

`domains/runs/services/run_events.py` (called per stdout line from
`domains/executors/claude_code.py` `_pump`).
- Replace the synchronous `redis.Redis` publish with an **async** client
  (`redis.asyncio`) so `publish_delta` doesn't block the loop.
- Offload the blocking file `open(...,"a")`/`write`/`stat` in `append_event` to a
  thread (`await asyncio.to_thread(...)`) or batch lines.
- Keep the best-effort/never-break-a-run posture (swallow errors, cooldown).
- Test: `publish_delta`/`append_event` do not call blocking Redis/file APIs on the
  loop (assert via a monkeypatched blocking-detector, or structural test).

Perf check: drive a streaming run locally and confirm the loop isn't stalled
(the /verify skill or a manual Ask run).

---

## Phase 3 — G1: make OpenCode write-capable (local-LLM delivery loop)

Codex is explicitly OUT of scope (its app-server auth path isn't working). The
goal here is the **local-LLM** delivery loop via **OpenCode**, which matters for
running the implement/fix/review loop on a local model with zero marginal cost.

`domains/runs/services/lifecycle.py:1179` (`_WRITE_CAPABLE_EXECUTORS`).
- Add `Executor.OPENCODE` to the write-capable set. OpenCode already runs with
  `cwd` set to the sandbox clone and edits files via its own tools (transport
  "local CLI + cwd"), and its generated `opencode.json` already wires the
  OpenSweep MCP server — so it can call `complete_run` and reach the platform
  tools, which is what the finalize path keys on.
- The delivery services reference `Executor.CLAUDE_CODE` in a few spots
  (`implement_run_service.py:355`, `fix_run_service.py:216`,
  `review_run_service.py:274`, `verification_run_service.py:197`). Resolve the
  write-capable executor from the provider/policy instead of the hardcoded
  constant, so an OpenCode provider drives implement/fix/review/verify.
- Confirm the OpenCode adapter's completion signal (MCP `complete_run` +
  session/artifact refs) satisfies `finalize_write_run` (write gate → push →
  draft PR). If a gap exists, close it in the OpenCode adapter (not by special-
  casing the executor).
- Test: a fix/implement dispatch with an OpenCode provider is accepted (not
  rejected by `_provider_supports_write`), and finalize runs.

---

## Phase 4 — G2: the merge button (close the loop)

### Backend
`domains/delivery/services/` + `api/v1/delivery.py`
- New service `merge_pull_request(pr_uid, *, actor_uid, override=False)`:
  1. Load PR; compute convergence (`convergence.compute_convergence` /
     `get_convergence_state`). Refuse (409) unless CONVERGED or `override` +
     maintainer.
  2. Merge via the git provider (`infrastructure/git_providers` — add a
     `merge_pull_request(owner, repo, number, method)` method; GitHub PUT
     `/pulls/{n}/merge`). Squash by default (make method a MergePolicy field).
  3. On success: `TicketService.mark_done_via_merge` / transition the linked
     ticket to `done` (the system move already exists — reuse it), update the
     PR mirror to merged.
  4. Audit `delivery.pr_merged`.
- Route: `POST /api/v1/delivery/pull-requests/{uid}/merge`,
  `require_role("maintainer")`. (Do NOT expose on the platform-tool/agent mount
  unless the repo opted into autonomy — mirror the ticket-transition gate.)
- Tests: merge-when-converged succeeds + moves the ticket; merge-when-not-converged
  is 409; the git provider merge is called with the right args (mock).

### Frontend
`front_end/src/views/PullRequestDetailView.vue` + `stores/deliveryStore.ts`
- Add a **Merge** button, enabled only when the convergence state is CONVERGED
  (reuse `ConvergenceChecklist` state). Wire to the endpoint; on success, toast +
  reflect the PR merged + ticket done. Handle the 409 (not converged) path.
- This is the README's promised second human action ("approve tickets and merge
  PRs") — today it dead-ends at an external GitHub link.

---

## Phase 5 — P7: multi-replica + deploy-safe (LARGE — consider a separate pass)

Today the stack assumes exactly one backend + one worker (`Dockerfile.prod`
`--workers 1`; `run_reconciliation.py:180-182` hardcodes the assumption; agent
pipelines run as `asyncio.create_task` inside the API process; the GitHub OAuth
pending-state is a JSON file). Consequences: no horizontal scale, and every
deploy kills in-flight runs.

- **P7a — pipelines off the API loop**: ensure Ask/Sweep/dispatch runs execute in
  the Celery **worker**, never `asyncio.create_task` in the API process
  (`lifecycle.py:438`). The API enqueues; the worker runs.
- **P7b — run resumption/checkpointing**: on process restart, re-enqueue
  resumable runs (persisted sandbox + `cli_session_id`) instead of failing them.
  Reuse the paused/resume machinery. Define what state a run needs to resume.
- **P7c — lease-based reconciliation**: replace "fail every run of my role on
  startup" with a per-run **heartbeat/lease** (owner writes `last_seen`); only
  reap runs whose lease is truly stale. A second replica must NOT reap its
  sibling's live runs. (This is where Phase 0's lock + a heartbeat field pay off.)
- **P7d — OAuth pending-state off the filesystem**: move the `github_app.py:97,111`
  JSON ledger to Redis/Neo4j (short-TTL keys).
- **P7e — scale the process count**: `--workers >1`, then hunt remaining
  single-process assumptions (in-memory caches, module-level dicts, per-loop
  singletons in `redis_client.py`).

P7 needs its own test matrix (two-replica simulation) and careful staging. **Do
not rush it into the same PR as B/C** — recommend landing Phases 0–4, then P7.

### P7 — landed vs. residual (this pass)

**Landed — P7c (lease-/liveness-safe reconciliation), the actual multi-replica
crux.** `reconcile_orphaned_runs` used to fail EVERY run stamped with the
restarting role on the single-process assumption — with 2+ replicas that reaps a
live sibling's in-flight run (data loss). Now gated by `OPENSWEEP_SINGLE_REPLICA`
(default `True` = unchanged fast-fail). Set it `False` for 2+ replicas and the
startup sweep reaps a same-role run ONLY once its transcript has gone quiet past
the grace window, so a restarting replica never kills a live sibling's run.
Tests in `test_run_reconciliation.py`. This is the one P7 sub-part that is
catastrophic if wrong and safe to land now behind a default-off flag.

**Residual (documented, NOT landed — each needs a two-replica test bench):**

- **P7a — pipelines off the API loop.** `lifecycle.py` still runs non-worker
  dispatches as `asyncio.create_task` in the API process (killed on deploy). The
  worker-execute path already exists (`get_role() == WORKER` enqueues
  `dispatch_run`); the change is to make the API ALWAYS enqueue. Deferred: it
  shifts every Ask/Sweep latency profile and needs a live drive to confirm no
  regression in the dev flow.
- **P7b — run resumption/checkpointing.** On restart, re-enqueue resumable runs
  (persisted sandbox + `cli_session_id`) instead of failing them. Needs a
  defined "resumable" predicate and reuse of the paused/resume machinery.
- **P7d — OAuth pending-state off the filesystem.** `github_app.py` keeps the
  OAuth pending-state in a process-local JSON file; move it to Redis (short-TTL
  keys) so any replica can complete the callback. Self-contained but touches the
  login round-trip — verify with a live OAuth flow, not just unit tests.
- **P7e — scale the process count.** `Dockerfile.prod --workers 1` → `>1`, then
  hunt remaining single-process assumptions (module-level caches, per-loop
  singletons). Only meaningful once P7a/b/d land; gated by
  `OPENSWEEP_SINGLE_REPLICA=False`.

With P7c's flag in place, flipping `OPENSWEEP_SINGLE_REPLICA=False` is safe from
the reconciliation-reaping standpoint; P7a/b/d/e remain before true horizontal
scale.

---

## Testing & verification (every phase)

- Unit tests per phase (listed above). Keep the suite green.
- Run the backend suite ignoring the 3 known pre-existing non-hermetic files
  (`test_github_repo_selection.py`, `test_github_app.py`, `test_auth_middleware.py`)
  with a clean env + randomized `PYTHONHASHSEED`:
  ```
  docker exec -e GITHUB_TOKEN= -e ZITADEL_PROJECT_ID= -e OPENSWEEP_AUTH_TOKEN= \
    -e PYTHONHASHSEED=random opensweep_backend python -m pytest -q \
    --ignore=tests/test_github_repo_selection.py \
    --ignore=tests/test_github_app.py \
    --ignore=tests/test_auth_middleware.py
  ```
- Frontend (G2): `cd front_end && npm run type-check`.
- `/verify` or a live Ask/merge drive for the runtime-affecting phases (P3, G2).

---

## Merge coordination — RESOLVED

Pass 1–3 + the agent-autonomy MCP feature are already committed to `main`
(`d25af3b`) and this branch is even with it, so there is nothing to coordinate:
the files this pass edits (`run_dispatch.py`, `lifecycle.py`, `claude_code.py`,
etc.) already carry their Pass 1–3 changes. Build on top and open a normal PR
against `main` when Phases 0–4 are done.

---

## Landing order (one autonomous pass)

1. Phase 0 (lock) → tests.
2. Phase 1 (B6/B7/B8) → tests.
3. Phase 2 (P3) → tests + perf check.
4. Phase 3 (G1 = OpenCode write-capable) → tests.
5. Phase 4 (G2 merge button) → backend tests + frontend type-check.
6. Phase 5 (P7) → sub-parts P7a–e, each verified; residual documented.
7. Full suite green (minus the 3 known non-hermetic files) → commit → open PR.
