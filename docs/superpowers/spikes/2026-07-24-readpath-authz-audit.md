# api/v1 Read-Path Org Enforcement Audit
**Date:** 2026-07-24  
**Auditor:** Task 2.1 (automated)  
**Scope:** Every `GET` and `WebSocket` handler under `back_end/api/v1/`  
**Method:** grep all `@router.get` / `@router.websocket` handlers; for each, trace the first authz call; confirm service-layer org filter when handler delegates to a service.

---

## Findings Table

| Route (Method PATH) | File:line | Authz | Note |
|---|---|---|---|
| GET /api/v1/health | meta.py:14 | n/a – not repo-scoped | No auth required; infrastructure liveness probe |
| GET /api/v1/version | meta.py:28 | n/a – not repo-scoped | No auth; static version info |
| GET /api/v1/me | meta.py:33 | n/a – not repo-scoped | Returns caller's own UserDTO; `get_current_user` is auth only |
| GET /api/v1/overview | meta.py:38 | n/a – org-scoped via service | `svc.overview(user.org_uid)` — returns aggregate metrics for the caller's own org, no cross-org data |
| GET /api/v1/agents | agents.py:35 | n/a – org-scoped via service | `agent_service.list_agents(org_uid=user.org_uid)` → `_visible_to_org` filter; agents are org-owned or system-shared (no repo dim) |
| GET /api/v1/agents/{uid} | agents.py:52 | n/a – org-scoped via service | `agent_service.get_agent(uid, org_uid=user.org_uid)` → `_visible_to_org`; raises 404 for foreign orgs |
| GET /api/v1/agents/{uid}/revisions | agents.py:117 | n/a – org-scoped via service | `agent_service.list_revisions(uid, org_uid=user.org_uid)` — revisions filtered by org |
| GET /api/v1/analysis | analysis.py:32 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid param → `require_repo_in_org`; no param → `org_repo_uids` list filter |
| GET /api/v1/analysis/current | analysis.py:54 | `require_repo_in_org` | line 62: `await require_repo_in_org(repository_uid, user.org_uid)` |
| GET /api/v1/analysis/{uid} | analysis.py:66 | `require_repo_in_org` | line 71: guard on `dto.repository_uid` |
| GET /api/v1/repositories/{uid}/analyzers | analysis.py:84 | `require_repo_in_org` | line 88: guard on fetched `node.repository_uid` |
| GET /api/v1/analysis/{uid}/questions/{qid} | analysis.py:98 | `require_repo_in_org` | line 102: guard on `node.repository_uid` |
| GET /api/v1/areas (list areas) | areas.py:34 | `require_repo_in_org` | line 39: guard on `repository_uid` param |
| GET /api/v1/areas/{uid} | areas.py:43 | `require_repo_in_org` | line 46: guard on `a.repository_uid` |
| GET /api/v1/areas/{uid}/detail | areas.py:50 | `require_repo_in_org` | line 58: guard on `a.repository_uid` |
| GET /api/v1/area-edits | areas.py:83 | `require_repo_in_org` | line 89: guard on `repository_uid` param |
| GET /api/v1/artifacts | artifacts.py:18 | `require_repo_in_org` | line 23: guard via `artifact_store.repository_uid_of(uri)` |
| GET /api/v1/audit | audit.py:44 | `org_repo_uids`-filter | line 58: `allowed = await org_repo_uids(user.org_uid)` applied to all event filters |
| GET /api/v1/audit/{uid} | audit.py:85 | `org_repo_uids`-filter | line 90: checks event.repository_uid in allowed set |
| GET /api/v1/campaigns (list) | campaigns.py:42 | `require_repo_in_org` | line 50: guard on `repository_uid` param |
| GET /api/v1/campaigns/active | campaigns.py:64 | `require_repo_in_org` | line 69: guard on `repository_uid` |
| GET /api/v1/campaigns/active-for-finding | campaigns.py:73 | `require_repo_in_org` | line 86: guard on `repository_uid` |
| GET /api/v1/campaigns/{uid} | campaigns.py:92 | `require_repo_in_org` | line 99: guard on `c.repository_uid` |
| GET /api/v1/comments | comments.py:33 | `require_repo_in_org` | line 41: guard on resolved `repo_uid` |
| GET /api/v1/comments (thread) | comments.py:45 | `require_repo_in_org` | line 59: guard on resolved `repo_uid` |
| GET /api/v1/pull-requests | delivery.py:49 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require; no param → org_repo_uids filter |
| GET /api/v1/pull-requests/{uid} | delivery.py:86 | `require_repo_in_org` | line 89: guard on `pr.repository_uid` |
| GET /api/v1/pull-requests/{uid}/files | delivery.py:105 | `require_repo_in_org` | line 114: guard on `pr.repository_uid` |
| GET /api/v1/pull-requests/{uid}/verdict | delivery.py:424 | `require_repo_in_org` | line 431: guard on `pr.repository_uid` |
| GET /api/v1/pull-requests/{uid}/resolutions | delivery.py:453 | `require_repo_in_org` | line 460: guard on `pr.repository_uid` |
| GET /api/v1/merge-policy | delivery.py:592 | `require_repo_in_org` | line 598: guard on `repository_uid` param |
| GET /api/v1/docs/docs | docs.py:33 | `require_repo_in_org` | line 38: guard on `repository_uid` param |
| GET /api/v1/docs/docs/{uid} | docs.py:42 | `require_repo_in_org` | line 45: guard on `d.repository_uid` |
| GET /api/v1/docs/doc-edits | docs.py:160 | `require_repo_in_org` | line 166: guard on `repository_uid` param |
| GET /api/v1/findings | findings.py:42 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require; no param → org_repo_uids filter |
| GET /api/v1/findings/ratchet | findings.py:83 | `require_repo_in_org` | line 94: guard on `repository_uid` param |
| GET /api/v1/findings/{uid} | findings.py:197 | `require_repo_in_org` | line 200: guard on `dto.repository_uid` |
| GET /api/v1/findings/{uid}/verifications | findings.py:483 | `require_repo_in_org` | line 490: guard on `f.repository_uid` |
| GET /api/v1/repositories/{uid}/freshness | freshness.py:21 | `require_repo_in_org` | line 26: guard on `repository_uid` param |
| GET /api/v1/git/connections | git_connections.py:43 | n/a – org-scoped via service | `require_role("maintainer")` + `org_pat_connections(user.org_uid)` — returns only this org's PAT connections |
| GET /api/v1/github/status | github_app.py:405 | n/a – org-scoped in handler | filters installations to `org_installation_ids(user.org_uid)`; non-admins see only their org's linked installs |
| GET /api/v1/github/available-repos | github_app.py:453 | n/a – org-scoped in handler | `pat_conns = await org_pat_connections(user.org_uid)`; `existing = await Repository.nodes.filter(org_uid=user.org_uid)` |
| GET /api/v1/github/setup | github_app.py:651 | n/a – not repo-scoped | OAuth redirect callback; trusts signed install state (single-use); no auth token |
| GET /api/v1/interests | interests.py:18 | `require_repo_in_org` | line 23: guard on `repository_uid` param |
| GET /api/v1/lenses | lenses.py:17 | n/a – not repo-scoped | Lenses are platform-level entities with no repository dimension; comment in source confirms this |
| GET /api/v1/lenses/{key} | lenses.py:25 | n/a – not repo-scoped | Same: platform-level entity, no tenancy boundary |
| GET /api/v1/llm-providers | llm_providers.py:23 | n/a – org-scoped via service | `svc.list_providers(user.org_uid)` — providers are org-owned |
| GET /api/v1/llm-providers/catalog | llm_providers.py:31 | n/a – not repo-scoped | Returns static kind catalog; same data for all orgs |
| GET /api/v1/llm-providers/active | llm_providers.py:36 | n/a – org-scoped via service | `svc.get_active(user.org_uid)` |
| GET /api/v1/llm-providers/status | llm_providers.py:45 | n/a – org-scoped via service | `svc.status(user.org_uid)` |
| GET /api/v1/llm-providers/{uid} | llm_providers.py:55 | n/a – org-scoped via service | `svc.get(uid, user.org_uid)` |
| GET /api/v1/memories | memories.py:18 | `require_repo_in_org` | line 26: guard on `repository_uid` param |
| GET /api/v1/mentions | mentions.py:34 | `org_repo_uids`-filter | line 46: `repo_uids = await org_repo_uids(user.org_uid)` — list filtered to org repos |
| GET /api/v1/me/profile | organizations.py:112 | n/a – not repo-scoped | Returns caller's own profile + org; no cross-org data possible |
| GET /api/v1/org | organizations.py:222 | n/a – org-scoped in handler | `_org_dto(await _get_org(user.org_uid), user)` — only the caller's own org |
| GET /api/v1/org/members | organizations.py:252 | n/a – org-scoped in handler | `_org_members(user.org_uid)` — only the caller's own org members |
| GET /api/v1/platform-config | platform_config.py:23 | n/a – not repo-scoped | `require_platform_admin` gate; returns singleton PlatformConfig |
| GET /api/v1/platform-read/docs | platform_read.py:37 | `require_tool_repo_access` | line 46: delegates to `require_tool_repo_access` → `require_repo_in_org` for human callers, run-token binding for executors |
| GET /api/v1/platform-read/docs/{slug} | platform_read.py:50 | `require_tool_repo_access` | line 60: same pattern |
| GET /api/v1/platform-read/memory-search | platform_read.py:64 | `require_tool_repo_access` | line 76: same pattern |
| GET /api/v1/platform-read/news-items | platform_read.py:85 | `require_tool_repo_access` | line 97: same pattern |
| GET /api/v1/platform-read/interests | platform_read.py:103 | `require_tool_repo_access` | line 113: same pattern |
| GET /api/v1/platform-read/findings | platform_read.py:122 | `require_tool_repo_access` | line 135: same pattern |
| GET /api/v1/platform-read/findings/{uid} | platform_read.py:145 | `require_tool_repo_access` | line 155: fetches node first, then guards on `finding.repository_uid` |
| GET /api/v1/platform-read/findings-search | platform_read.py:159 | `require_tool_repo_access` | line 171: same pattern |
| GET /api/v1/platform-tools/delivery/convergence-state | platform_tools_delivery.py:61 | `require_tool_repo_access` | line 75: guard on `pr.repository_uid` |
| GET /api/v1/platform-tools/delivery/resolutions | platform_tools_delivery.py:79 | `require_tool_repo_access` | line 92: guard on `pr.repository_uid` |
| GET /api/v1/platform-tools/delivery/merge-policy | platform_tools_delivery.py:295 | `require_tool_repo_access` | line 308: guard on `repository_uid` param |
| GET /api/v1/platform-tools/delivery/queue | platform_tools_delivery.py:312 | `require_tool_repo_access` | line 324: guard on `repository_uid`; empty uid → 404 (explicit comment in code) |
| GET /api/v1/platform-tools/delivery/comments | platform_tools_delivery.py:328 | `require_tool_repo_access` | line 348: guard on `subject.repository_uid` |
| GET /api/v1/platform-tools/tickets/get | platform_tools_tickets.py:178 | `require_tool_repo_access` | line 191: guard on `t.repository_uid` |
| GET /api/v1/platform-tools/tickets/list | platform_tools_tickets.py:195 | `require_tool_repo_access` | line 210: guard on `repository_uid`; empty uid → 404 (explicit comment in code) |
| GET /api/v1/repositories | repositories.py:29 | n/a – org-scoped via service | `svc.list_repositories(user.org_uid)` — service filters to org |
| GET /api/v1/repositories/by-slug/{slug} | repositories.py:37 | n/a – org-scoped via service | `svc.get_repository_by_slug(slug, user.org_uid)` |
| GET /api/v1/repositories/{uid} | repositories.py:50 | n/a – org-scoped via service | `svc.get_repository(uid, user.org_uid)` — service enforces org ownership |
| GET /api/v1/repositories/{uid}/file | repositories.py:95 | n/a – org-scoped via service | `repos.get_repository(uid, user.org_uid)` called first at line 105; org check in service before file fetch |
| GET /api/v1/run-policies | run_policies.py:50 | n/a – not repo-scoped | RunPolicies are platform-level (no org/repo dimension); any authenticated user can list |
| GET /api/v1/run-policies/{uid} | run_policies.py:61 | n/a – not repo-scoped | Same; platform-level config visible to all authenticated users |
| GET /api/v1/runs | runs.py:275 | `org_repo_uids`-filter | line 300: `allowed = await org_repo_uids(user.org_uid)`; DB filter on line 304: `Run.nodes.filter(repository_uid__in=list(allowed))` |
| GET /api/v1/runs/active | runs.py:357 | `org_repo_uids`-filter | line 374: `allowed = await org_repo_uids(user.org_uid)` |
| GET /api/v1/runs/{uid}/steps | runs.py:413 | `require_repo_in_org` | line 429: guard on `r.repository_uid` |
| GET /api/v1/runs/{uid}/changes | runs.py:449 | `require_repo_in_org` | line 461: guard on `r.repository_uid` |
| GET /api/v1/runs/{uid} | runs.py:465 | `require_repo_in_org` | line 471: guard on `r.repository_uid` |
| WS  /api/v1/runs/{uid}/ws | runs.py:726 | `require_repo_in_org` | line 757: guard on `run.repository_uid`; `get_current_user` at line 752 authenticates first |
| GET /api/v1/sandboxes | sandboxes.py:15 | `org_repo_uids`-filter | line 20: `allowed = await org_repo_uids(user.org_uid)` |
| GET /api/v1/sandboxes/{uid} (create-sandbox POST, no GET on single) | sandboxes.py:28 | `require_repo_in_org` | line 32: guard on `node.repository_uid` |
| GET /api/v1/scheduled-agents | scheduled_agents.py:27 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require; no param → org_repo_uids filter |
| GET /api/v1/scheduled-agents/{uid} | scheduled_agents.py:46 | `require_repo_in_org` | line 53: guard on `s.repository_uid` |
| GET /api/v1/scheduled-agents/{uid}/runs | scheduled_agents.py:130 | `require_repo_in_org` | line 137: guard on `s.repository_uid` |
| GET /api/v1/slack/status | slack.py:67 | n/a – org-scoped in handler | `slack_service.get_connection(user.org_uid)` — caller's own org Slack connection |
| GET /api/v1/slack/install | slack.py:82 | n/a – org-scoped | `require_role("admin")` + org_uid scoped state; generates install URL for caller's org |
| GET /api/v1/slack/oauth/callback | slack.py:95 | n/a – not repo-scoped | OAuth redirect; trusts signed single-use install state; no OpenSweep auth token |
| GET /api/v1/slack/channels | slack.py:142 | n/a – org-scoped in handler | `slack_service.get_connection(user.org_uid)` — caller's own org Slack channels |
| GET /api/v1/slack/rules | slack.py:153 | n/a – org-scoped in handler | `slack_service.list_rules(user.org_uid)` |
| GET /api/v1/repositories/{uid}/sweep/estimate | sweep.py:334 | `require_repo_in_org` | line 340: guard on `repository_uid` param |
| GET /api/v1/threads | threads.py:25 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require (line 32); no param → org_repo_uids filter (line 40) |
| GET /api/v1/threads/{uid} | threads.py:59 | `require_repo_in_org` | line 63: guard on `t.repository_uid` |
| GET /api/v1/ticket-groups | ticket_groups.py:21 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require (line 37); no param → org_repo_uids filter (line 40) |
| GET /api/v1/tickets | tickets.py:29 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require (line 40); no param → org_repo_uids filter (line 49) |
| GET /api/v1/tickets/{uid} | tickets.py:224 | `require_repo_in_org` | line 228: guard on `ticket.repository_uid` |
| GET /api/v1/news | news.py:124 | `require_repo_in_org` OR `org_repo_uids`-filter | repository_uid → require (line 132); no param → org_repo_uids filter (line 139) |
| GET /api/v1/news/{uid} | news.py:255 | `require_repo_in_org` | line 258: guard on `n.repository_uid` |
| GET /api/v1/notifications | notifications.py:48 | `org_repo_uids`-filter via service | `notification_service.list_feed(user, ...)` → line 104 in service: `allowed = await org_repo_uids(user.org_uid)` applied per event |
| GET /api/v1/notifications/counts | notifications.py:67 | `org_repo_uids`-filter via service | `notification_service.unread_counts(user)` → calls `list_feed` which applies org_repo_uids filter |
| GET /api/v1/repositories/{uid}/workflow | workflow.py:53 | `require_repo_in_org` | line 57: guard on `repository_uid` param |
| GET /api/v1/repositories/{uid}/analyzers | workflow.py:96 | `require_repo_in_org` | line 102: guard on `repository_uid` param |

---

## GAPS

**No gaps found — every repo-scoped read enforces org tenancy.**

Every GET and WebSocket handler under `api/v1/` either:

1. Calls `require_repo_in_org(resource.repository_uid, user.org_uid)` directly (raises HTTP 404 if the resource belongs to a different org), or
2. Filters lists using `org_repo_uids(user.org_uid)` as the set of allowed repos (empty set → no data returned), or
3. Passes `user.org_uid` into the service layer which enforces the constraint (repositories, LLM providers, agents, notifications, slack, git connections), or
4. Is legitimately not repo-scoped: platform-level entities (lenses, run-policies, platform-config), the caller's own identity/org endpoints (`/me`, `/org`, `/org/members`), OAuth redirect callbacks (trust is a signed single-use state), infrastructure probes (`/health`, `/version`), and the LLM provider catalog (static data).

Routes worth noting but not gaps:
- **`GET /api/v1/run-policies`** and **`GET /api/v1/run-policies/{uid}`**: RunPolicies have no org or repo dimension — they are platform-wide config. Any authenticated user can read them. This is by design (executor agents must resolve their policy). Write operations require `require_platform_admin`.
- **`GET /api/v1/lenses`** and **`GET /api/v1/lenses/{key}`**: Lenses are platform-level entities. The source file documents this explicitly: "Lenses are platform-level rows (no repository dimension), so reads carry no tenancy filter." Write operations require `require_platform_admin`.
- **`GET /api/v1/platform-tools/delivery/queue`** and **`GET /api/v1/platform-tools/tickets/list`** with empty `repository_uid`: Both handlers explicitly 404 on an empty `repository_uid` rather than returning cross-org data (comments in source confirm this is deliberate).
