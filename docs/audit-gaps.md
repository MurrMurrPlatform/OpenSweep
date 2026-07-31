# External audit — findings NOT present in the OpenSweep backlog

Cross-referenced against all 223 Tickets and 227 Findings in the platform
(2026-07-31). Findings already ticketed are listed at the bottom and should
not be re-filed.

Legend: **[C]** critical · **[H]** high · **[M]** medium · **[L]** low

---

## 1. Security — none of these are ticketed

| # | Severity | Finding | Location |
|---|---|---|---|
| S1 | **C** | Any self-signup user gets `role="admin"`, and `require_role("admin")` gates provider creation, where `cli_command_template` is an unvalidated `str` that reaches `create_subprocess_exec` with unrestricted `argv[0]` → RCE on the shared worker | `oidc_user.py:120-128` → `llm_providers.py:66` → `schemas.py:38,67,85` → `llm_executor.py:201` |
| S2 | **C** | No isolation boundary: the "sandbox" is a plain directory, the agent CLI is a direct child of the worker running `--permission-mode bypassPermissions` as root, and `/proc/1/environ` defeats the env allowlist. `~/.claude` + `~/.codex` are bind-mounted **read-write** from the host | `sandbox_service.py:129`, `agent_env.py:21-34`, `docker-compose.yml:101-104` |
| S3 | **C** | Stored XSS: `MdPreview` rendered with no `sanitize` prop, markdown-it configured `html:true`, no sanitizer dependency in the project — across 17 call sites rendering agent/repo content. OIDC user (incl. `offline_access` refresh token) is in `localStorage` | `MarkdownView.vue:48`, `auth.ts:40` |
| S4 | **H** | IDOR: `POST /runs` validates `repository_uid` but passes `linked_pr_uid` / `linked_ticket_uid` / `linked_finding_uid` through unchecked; `run_context` fetches each with a bare `get_or_none` and renders it into the agent context and transcript | `runs.py:106-108,201-203`, `run_context.py:134-160` |
| S5 | **H** | Org invitations match on an **unverified** email (`email_verified` never checked anywhere) and have no expiry | `oidc_user.py:71-86`, `organizations/models.py:51-67` |
| S6 | **H** | SSRF: provider `base_url` is unvalidated and the response body is written into the run transcript (`inv.raw_output = resp.text`). Your own `_assert_public_http_url` guard exists 300 lines away and is not reused | `schemas.py:64,82`, `llm_executor.py:664-666` |
| S7 | **H** | Write gate validates `result.work_branch` but pushes the *caller's* `work_branch` variable; and the promised `opensweep/` prefix is never enforced — only `main`/`master`/`develop` are blocked | `write_gate.py:144-146`, `run_dispatch.py:195-200`, `write_gate.py:82-86` |
| S8 | **H** | Cross-tenant agent tampering: `Agent.org_uid=""` means globally visible, writes gate on `provenance` not `org_uid`, so any org can rewrite a shared imported agent. Separately, agent mutation routes have **no role gate** — a `viewer` can rewrite an implement agent's prompt | `agents/models.py:92`, `agent_service.py:274-295`, `api/v1/agents.py:71-131` |
| S9 | **H** | Startup re-seal covers `LLMProvider` only — a `GitConnection` PAT or `SlackConnection` token written while the key was unset stays plaintext forever. Empty `OPENSWEEP_SECRETS_KEY` is a *warning*, not an error, so boot is not blocked | `credentials.py:30-62`, `production_guards.py:140-145` |
| S10 | **M** | opencode provider API key written to a predictable `/tmp` path with default umask (world-readable). The codex path 40 lines away correctly uses `0600`/`0700` | `llm_executor.py:398-412` vs `runtime_env.py:90,116` |
| S11 | **M** | Run tokens (`osrt_`) are deterministic `HMAC(secret, run_uid)` with no expiry, no revocation, and no run-state check — a leaked token grants perpetual write on that repo's platform-tool surface | `run_tokens.py:45-51`, `platform_scope.py:32-55` |
| S12 | **M** | `secretbox` derives the Fernet key with a single unsalted SHA-256 of an operator passphrase — offline-brute-forceable, no domain separation | `secretbox.py:38-39` |
| S13 | **M** | Unquoted `{{instruction}}` / `{{system_prompt}}` template variants exist alongside the `_q` shlex-quoted ones. All shipped defaults use `_q`, so this is a live footgun, not a live bug | `llm_executor.py:479-483` |
| S14 | **M** | OIDC audience pin only enforced when `ENVIRONMENT=production` — staging accepts any audience from the issuer, and `zitadel_roles` then reads roles from any project | `oidc.py:106-116`, `production_guards.py:94-105` |
| S15 | **M** | `RunPolicy` has **no `org_uid` field at all**; `run_policies.py:55` is a bare `nodes.all()` behind plain `get_current_user` — every org reads every other org's cost ceilings and model routing | `run_policies/models.py`, `api/v1/run_policies.py:55,62` |
| S16 | **L** | `artifact_store._safe` permits `.`; `_path_from_uri` has no `resolve()`/`is_relative_to(root)` check. Bounded by the `require_repo_in_org` on segment 0, so not currently reachable cross-tenant | `artifact_store.py:37-38,64-72` |
| S17 | **L** | `default_branch` is free-form and reaches `git checkout <branch>` bare — a leading `-` parses as an option. No `--` separator | `repositories/schemas.py:25`, `sandbox_service.py:215` |
| S18 | **L** | No log-redaction backstop; `command_excerpt` is built from full argv which for codex includes the run token in a header. Currently never persisted or logged — latent, not live | `logging_config.py:43`, `llm_executor.py:225`, `mcp_bridge.py:96-98` |

---

## 2. Correctness — not ticketed

| # | Finding | Location |
|---|---|---|
| B1 | `cli_usage` is not reset between continuation passes — a pass killed without a `result` line re-adds the previous pass's turns and dollars. Runs can be stamped `LIMIT_EXCEEDED` at under half their real budget | `claude_code.py:195,258-259,320-322,372-374` |
| B2 | **The daily dollar cap never fires.** `policy_resolver` sums `usage["dollars"]`; `claude_code` writes `usage["dollars_used"]`. Claude Code is the only write-capable executor, so the money ceiling is inert on the entire path that spends money | `policy_resolver.py:182` vs `claude_code.py:504` |
| B3 | An agent calling `complete_run(final_status="failed")` makes `dispatch_result_is_stale` return True, so `on_turn_complete` never runs — no write gate, no push, no PR. Commits sit in the sandbox until retention expiry | `lifecycle.py:76-94,781-795`, `complete_run.py:109-111` |
| B4 | A write run stopped by its own wall/turn budget returns `LIMIT_EXCEEDED`, which skips gate-and-push entirely **and** does not refund the fix round (only `prep_failed` refunds). Three rounds can burn with zero commits delivered | `run_dispatch.py:157-166`, `claude_code.py:474-481`, `fix_run_service.py:484-486` |
| B5 | Queued time counts against the liveness timeout — a run backlogged in Redis past 900s is failed with a misleading "process crashed", then silently skipped when the worker picks it up | `run_reconciliation.py:30,39-56,126-155`, `task_limits.py:23-34` |
| B6 | `_DISPATCH_LOCKS` is a per-process dict, but both the FastAPI backend and the Celery worker dispatch runs — the claimed read-then-write atomicity does not hold across processes. Two fix rounds, two sandboxes, two racing pushes | `run_dispatch.py:44,70-94` |
| B7 | Quota resume has no lease — beat re-enqueues every `paused_quota` run every 10 min, and status isn't flipped to RUNNING until after provider selection and a full clone. Two workers can resume the same run | `resume_paused.py:48-61,86-101`, `lifecycle.py:924-1035` |
| B8 | `execute_queued_run` does the entire sandbox clone *before* flipping the row to RUNNING, so under `task_acks_late` a redelivered task clones and dispatches a second agent for the same run row | `lifecycle.py:443-493,558-587` |
| B9 | A write sandbox created via `sandbox_factory` is orphaned when the run leaves QUEUED mid-clone — the cleanup branch excludes it, `Run.sandbox_uid` stays empty, and the fix round is never refunded | `lifecycle.py:561-572` |
| B10 | Any exception syncing *any* PR aborts the whole webhook handler with a 500 on the stated assumption "GitHub will redeliver" — **GitHub does not auto-redeliver.** `_delivery_follow_through` is the only place auto-review is dispatched, so it is silently lost | `github_webhooks.py:476-494,242-258` |
| B11 | In-loop quota detection lacks the success exemption the post-loop check has — an agent that merely *writes the words* "quota exceeded" in its closing summary ends the run early with no verdict | `claude_code.py:329-331` vs `:420-427` |
| B12 | `_thread_conversation_for_pr` swallows all exceptions and returns `None`; the caller reads that as "no thread" and dispatches a cold fix run on a live thread's branch → racing pushes on the same ref | `fix_run_service.py:309-323,126-130` |
| B13 | *(uncommitted tree)* `repository_uid: Optional[str] = Query(None)` — `bool(Query(None))` is **True**, so the new filter drops every event. It broke the audit **tenancy** test; had the assertion been `not in`, it would pass while asserting nothing | `api/v1/audit.py` |
| B14 | *(uncommitted tree)* `sorted(set(scopes), key=len)` — equal-length siblings land in hash-seed order. Output is **nondeterministic in production**, ~40% flaky in CI | `area_health.py:75` |

---

## 3. Performance & scale — not ticketed

| # | Finding | Location |
|---|---|---|
| P1 | **No `visibility_timeout` configured** with `task_acks_late=True` → Redis default 3600s, while runs may take 24h. Every run over 60 min is redelivered; a `deep` run spawns ~4 concurrent copies | `celery_app.py:43-113` |
| P2 | **`install_labels` is never called.** 193 model-level `index=True` are decorative. Verified live: `Area`, `Doc`, `Thread`, `Checked`, `Memory`, `Lens`, `EpicProposal`, `AreaEdit`, `DocEdit`, `OAuthToken` have **zero** indexes — not even `uid`. (The dead `TicketGroupProposal` label has 3; the live `EpicProposal` that replaced it has none) | `neomodel_bootstrap.py:68-142` |
| P3 | Synchronous `redis.Redis.publish()` + blocking file append on the async event loop, **once per line of agent stdout**, `socket_timeout=0.5` | `run_events.py:74-92` ← `claude_code.py:252,260` |
| P4 | Single Celery queue: hour-long agent runs starve the 60s beat ticks (campaign chaining, sandbox cleanup, Slack delivery). No `task_routes` | `celery_app.py:43-113` |
| P5 | 878 KB of markdown editor (4× the entire app bundle) statically imported into `ShellLayout` — loads on every page including login. One `defineAsyncComponent` | `ShellLayout.vue:9,67`, `MarkdownView.vue:3-4` |
| P6 | `RunTranscript` folds the event stream O(n²) (`[...out].reverse().find()` per event), and `RunDetailView` replaces the array identity per socket frame → O(n³) to watch one run | `RunTranscript.vue:62,67`, `RunDetailView.vue:157` |
| P7 | Single-replica architecture: `--workers 1`, agent pipelines run inside the API event loop, orphan reconciliation fails any run stamped with its role on startup, and the GitHub OAuth pending-state ledger is a JSON file. Every deploy kills in-flight runs; there is no resumption | `lifecycle.py:438`, `run_reconciliation.py:180-182`, `github_app.py:97,111` |
| P8 | Three independent polling timers (5s/5s/6s); `useActiveRuns` has **no `document.hidden` guard**, so backgrounded tabs poll forever. ~34 req/min per tab on four views | `useActiveRuns.ts:6,63`, `useRunNotifications.ts:10,127`, `NotificationBell.vue:18,42` |
| P9 | Full GitHub file tree fetched uncached (`get_tree?recursive=1`) on the Areas board, area detail, campaign planning, **and an MCP tool agents call** | `file_tree.py:35,42`, `areas_tools.py:39` |
| P10 | Undebounced full-collection filtering on Findings and Tickets, rebuilding a multi-KB search string per finding per keystroke, with no virtualization and no `limit` on fetch | `FindingsView.vue:236-260,759`, `TicketsView.vue:197-210` |
| P11 | `AreasView` calls O(n) helpers from inside the template's `v-for` (`hasChildren`, `staleUnder` ×3 per row, `groupFileTotal`); server-side `stale_descendants` is O(areas²) | `AreasView.vue:528,533,183,459`, `area_health.py:208-212` |
| P12 | `metrics_service` does 5 sequential Cypher round-trips **per repository** behind `GET /overview`; bulk finding delete does `get_node` then `get_or_none` again per uid (~3 round trips × N) | `metrics_service.py:126-153`, `findings.py:180-183` |

---

## 4. Product / implementation gaps — not ticketed

| # | Finding | Location |
|---|---|---|
| G1 | **`_WRITE_CAPABLE_EXECUTORS = frozenset({CLAUDE_CODE})`** — "bring your own agent" is false for the entire delivery loop. An OpenAI/Ollama-only user gets discovery and no PR loop | `lifecycle.py:1179` |
| G2 | **There is no merge action anywhere in the app.** The convergence predicate terminates in a green badge and an `<a target="_blank">` to GitHub. Nothing moves a ticket to `done` | `PullRequestDetailView.vue:496-551` |
| G3 | Missing GitHub credentials produce a silent no-op — branch pushed, no PR, empty uid returned; converged status skipped; review skipped. Comments still reference a "mock store" that no longer exists | `implement_run_service.py:475-476`, `pull_request_service.py:461-462`, `github_client.py:6-7` |
| G4 | Org invitations send **no email** — there is no mail transport in the backend. The invitee only discovers it by independently signing up | `api/v1/organizations.py:363-390` |
| G5 | `policy.dry_run` round-trips DTO↔model and is editable in the UI, but nothing at dispatch reads it | `run_policies/` |
| G6 | `GET /repositories/{uid}/freshness` has a store method and zero UI consumers — per-doc "Checked stamps", a marketed concept, is API-only | `docStore.ts:192,257` |
| G7 | `TicketImplementButton` is imported only by `TicketCard` — approve a ticket on its detail page and there is nothing to click. It also renders *nothing* (not a disabled button with a reason) for backlog/epic-member tickets | `TicketImplementButton.vue:37-41`, `TicketCard.vue:365` |
| G8 | WebSocket gives up after ~7s (`MAX_RETRIES=3`), then displays `offline · REST fallback` when **there is no fallback receive path** — the transcript freezes while the working indicator keeps animating | `useRunSocket.ts:67-68`, `RunDetailView.vue:100` |
| G9 | No progress bar, ETA, elapsed timer, or queue position anywhere — for operations that run 10+ minutes and cost money. `RunsView` has no duration column | `RunsView.vue`, `ActiveRunChip.vue:23-27` |
| G10 | Provider `configured` means "an active row exists" — health is never consulted, and `WelcomeView` accepts *any* row. Paste a bad token, get a green checkmark, fail every run later with no persistent indicator | `llm_provider_service.py:90`, `WelcomeView.vue:70` |
| G11 | **Live in your running stack right now:** a repository points at a deleted `git_connection_uid` with no re-point path. PR sweep fails every 60s and the write gate blocks — surfaced only as a log warning and a silently incrementing error counter | `repositories/models.py:42`, `git_connections.py:133-148` |

---

## 5. Test / CI / supply chain — not ticketed

| # | Finding |
|---|---|
| T1 | `UPDATE_GOLDENS=1` makes 8 golden tests **silent green no-ops** (early `return` before any assertion) while rewriting tracked files. No CI guard. 5 of 8 goldens are dirty right now |
| T2 | Test suite is **not hermetic** — green in CI, 19 failures in the dev container, because pydantic loads the ambient `.env` |
| T3 | **Zero frontend tests.** No vitest/jest/playwright/@vue/test-utils, no `test` script, for 67k lines of Vue. Only gate is `vue-tsc` |
| T4 | `lifecycle.py` (1,187 LOC) has **no test file at all**; `run_dispatch.py` (218 LOC, fan-in of 5) has zero test references. `domains/delivery/` is **18% covered** — the lowest in the repo, and the half that has never worked |
| T5 | All 19 backend deps are unpinned `>=` floors; CI does a bare `pip install`. **A `uv.lock` already exists and nothing uses it.** `pytest` ships into the production image |
| T6 | Ruff is `continue-on-error` (advisory). 830 findings, but ~740 are mechanical — after `extend-immutable-calls` for `Depends`/`Query` and `--fix`, only ~27 are real bugs (F401/F841/F811/B904). Half a day to make it a gate |
| T7 | No dependency audit, no Dependabot/Renovate, no CodeQL/semgrep/bandit, no secret scanning, no coverage measurement (`pytest-cov` isn't installed), and `Dockerfile.prod` is never built in CI |
| T8 | Assertion-free tests with security/lifecycle names: `test_cli_env_allowlist.py:56` (*"no platform secret reaches the child"*), `test_process_tree.py:69`, plus 6 bare-`asyncio.run` thread-hook tests — two using `raising=False`, so a rename makes the patch a silent no-op |
| T9 | `test_read_path_tenancy.py` stubs `require_repo_in_org` to raise 404 then asserts a 404 — the real tenancy predicate never executes |
| T10 | Local Python is 3.13; CI runs 3.12 |

---

## Already ticketed — do not re-file

`publish_verdict_review` / `.github_number` AttributeError · `max_files_touched` no runtime enforcement · fire-and-forget thread delivery (×3) · `capacity.active_run_count` O(n) scan · notification top-300 cross-tenant window · the `nodes.all()`-then-Python-filter pattern (×8, incl. `checked_service`, `area_service`, docs, agents) · `submit_thread_plan` envelope tenancy gap · short `OPENSWEEP_SECRETS_KEY` plaintext in staging · Cypher label interpolation in `audit.py` · `attach_artifact` caller-supplied `repository_uid` bypass · `RunPolicy` budget fields unreachable in admin UI · blocking Redis/file I/O in `sandbox_service._notify_runs_workspace_expired` · sandbox clone has no timeout · destroying a sandbox mid-run · dual frontend lockfiles · cron dispatch / doc-freshness redelivery races · fix round burned without refund (thread-message variant)
