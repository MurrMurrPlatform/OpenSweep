// Generated against PLATFORM.md backend (v0.3.0).
// Core primitives: Agent/ScheduledAgent, Finding, Doc/Memory — plus RunPolicy and the
// platform tool surface.

// ── Enums ───────────────────────────────────────────────────────────────────

export type RepositoryMode = 'github'

export type FindingKind = 'defect' | 'improvement' | 'gap' | 'proposal' | 'observation' | 'feature-idea'
export type Severity = 'low' | 'medium' | 'high' | 'critical'
/** Fix-size estimate for a finding — distinct from AgentEffort (run compute tier). */
export type FindingSize = 'trivial' | 'small' | 'medium' | 'large'
export type FindingStatus =
  | 'open'
  | 'acknowledged'
  | 'wont-fix'
  | 'fixed'
  | 'accepted'
  | 'superseded'
  | 'dismissed'

export type SourcePath = 'tool-call' | 'parsed-blob' | 'raw-derived'
export type ParseStatus = 'ok' | 'degraded'

export type Executor = 'internal_llm' | 'claude_code' | 'codex' | 'opencode' | 'manual'
export type ExecutionMode = 'analyze_only' | 'implement'
export type Autonomy = 'disabled' | 'suggest' | 'ask-before-run' | 'auto-run-cheap' | 'auto-run-any'
export type AgentProvenance = 'system' | 'user' | 'imported'
/** V3 run states (PLATFORM_V3_DESIGN.md §2). 'awaiting_input' = turn done,
 *  follow-ups accepted; 'ended' = workspace destroyed (a follow-up reopens). */
export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_input'
  | 'ended'
  | 'failed'
  | 'cancelled'
  | 'limit_exceeded'
  | 'paused_quota'
export type RunPlaybook = 'chat' | 'ask' | 'review' | 'fix' | 'implement' | 'verify' | 'document' | 'refine'
export type RunTrigger = 'manual' | 'event' | 'schedule'
export type RunSurface = 'runs' | 'comment' | 'chat'
export type AgentEffort = 'short' | 'normal' | 'deep' | 'unlimited'
/** Reasoning level on agents/runs; '' = inherit from the effort tier. */
export type ReasoningLevel = '' | 'low' | 'medium' | 'high'

// ── Repository ──────────────────────────────────────────────────────────────

export interface RepositoryDTO {
  uid: string
  slug: string
  mode: RepositoryMode
  name: string
  description?: string
  default_branch?: string
  color_scheme?: string
  is_active?: boolean
  github_owner?: string | null
  github_repo?: string | null
  github_repo_id?: number | null
  github_installation_id?: number | null
  github_connection_status?: string | null
  last_synced_at?: string | null
  metadata?: Record<string, unknown>
  kill_switch_active?: boolean
  created_at?: string | null
  updated_at?: string | null
}

// ── GitHub App (env-configured — scripts/github-app-setup.sh, §7) ────────

export interface GitHubAppInstallation {
  id: number
  account: string
  repos_count?: number | null
}

/** Org PAT connection (kind="pat") — the self-serve token path. */
export interface PatConnectionInfo {
  uid: string
  kind: string
  account: string
  created_at: string
}

export interface GitHubAppStatus {
  /** The org can reach GitHub: a configured App and/or a PAT connection. */
  connected: boolean
  slug?: string
  html_url?: string
  app_id?: string
  install_url?: string
  installations: GitHubAppInstallation[]
  installations_error?: string
  pat_connections?: PatConnectionInfo[]
}

// ── GitHub App · available repositories (explicit connect flow) ─────────────

export interface AvailableRepo {
  owner: string
  name: string
  full_name: string
  repo_id: number
  default_branch: string
  private: boolean
  description?: string | null
  /** Already registered as a OpenSweep workspace. */
  registered: boolean
  /** UID of the OpenSweep repository when registered. */
  repository_uid?: string | null
}

/** One repo group in the connect dialog — a GitHub App installation (id set)
 *  or an org PAT connection (connection_uid set, id=0). */
export interface AvailableReposInstallation {
  id: number
  account: string
  connection_uid?: string
  /** Per-group fetch error (repos list is empty when set). */
  error?: string | null
  repos: AvailableRepo[]
}

export interface AvailableReposDTO {
  connected: boolean
  install_url?: string
  installations: AvailableReposInstallation[]
}

export interface RegisterRepoRequest {
  /** Exactly one of installation_id / connection_uid. */
  installation_id?: number
  connection_uid?: string
  owner: string
  name: string
}

export interface PlatformConfigDTO {
  global_kill_switch: boolean
  updated_at?: string | null
}

export interface FileContentDTO {
  path: string
  content: string
  language?: string | null
  total_lines: number
  start_line?: number
  end_line: number
  truncated?: boolean
  source: 'github' | 'missing'
  bytes_total?: number | null
}

// ── Finding ─────────────────────────────────────────────────────────────────

export interface FindingDTO {
  uid: string
  repository_uid: string
  /** Optional free-text tags (e.g. "security", "flaky-test"); replaces the concern enum. */
  tags: string[]
  kind: FindingKind
  severity: Severity
  size: FindingSize
  subtype: string
  title: string
  confidence: number
  /** Detailed markdown analysis: what is wrong, where, and how it manifests. */
  description: string
  /** Markdown: why the problem exists (mechanism, not symptom). */
  root_cause: string
  why_it_matters: string
  evidence: Record<string, unknown>
  suggested_fix: string
  affected_paths: string[]
  dedupe_key: string
  source_run_uid?: string | null
  /** Every run that filed or re-confirmed this finding. */
  source_run_uids?: string[]
  /** Last time any run re-found (confirmed) this finding. */
  last_confirmed_at?: string | null
  executor: string
  source_path: SourcePath
  parse_status: ParseStatus
  provider_uid?: string | null
  provider_label?: string
  provider_kind?: string
  provider_model?: string
  status: FindingStatus
  created_at?: string | null
  updated_at?: string | null
}

export interface FileFindingRequest {
  repository_uid: string
  tags?: string[]
  kind?: FindingKind
  severity?: Severity
  size?: FindingSize
  subtype?: string
  title: string
  confidence?: number
  description?: string
  root_cause?: string
  why_it_matters?: string
  evidence?: Record<string, unknown>
  suggested_fix?: string
  affected_paths?: string[]
  source_run_uid?: string | null
  executor?: string
}

/** Human correction of a finding's narrative/triage fields. Every field is
 *  optional — only those sent are applied. Status has its own transition
 *  routes and machine provenance stays immutable. */
export interface UpdateFindingRequest {
  tags?: string[]
  kind?: FindingKind
  severity?: Severity
  size?: FindingSize
  subtype?: string
  title?: string
  description?: string
  root_cause?: string
  why_it_matters?: string
  suggested_fix?: string
  affected_paths?: string[]
}

// ── News (external signal inbox → feature-idea Findings) ───────────────────

export type NewsSource = 'searxng' | 'github' | 'hackernews' | 'arxiv' | 'trendshift' | 'manual'
export type NewsCategory =
  | 'trending-repo'
  | 'ai-news'
  | 'framework'
  | 'technique'
  | 'research'
  | 'tooling'
  | 'industry'
export type NewsStatus = 'new' | 'saved' | 'dismissed' | 'converted'

export interface NewsItemDTO {
  uid: string
  repository_uid: string
  title: string
  url: string
  source: NewsSource
  category: NewsCategory
  /** Markdown digest of the item. */
  summary: string
  /** Markdown: why this matters for THIS repository. */
  relevance: string
  tags: string[]
  published_at?: string | null
  status: NewsStatus
  /** Non-empty once converted — uid of the feature-idea Finding. */
  converted_finding_uid: string
  dedupe_key: string
  source_run_uid?: string | null
  created_at?: string | null
  updated_at?: string | null
}

/** PATCH /news/{uid} — omitted fields stay unchanged. */
export interface UpdateNewsItemRequest {
  title?: string
  category?: NewsCategory
  summary?: string
  relevance?: string
  tags?: string[]
}

// ── Interest (per-repo watchlist steering news scans) ──────────────────────

export interface InterestDTO {
  uid: string
  repository_uid: string
  title: string
  details: string
  enabled: boolean
  created_at?: string | null
  updated_at?: string | null
}

// ── Agents + Scheduled agents + Runs ────────────────────────────────────────

/** What an Agent produces — the user-facing replacement for playbooks. */
export type ProducesKind =
  | 'findings'
  | 'answer'
  | 'documentation'
  | 'doc-tree'
  | 'analysis'
  | 'review-verdict'
  | 'verification'
  | 'code-changes'

/** An org-scoped, versioned agent definition (system rows are shared). */
export interface AgentDTO {
  uid: string
  title: string
  description: string
  prompt: string
  produces: ProducesKind
  default_effort: AgentEffort
  /** '' = inherit the effort tier's default reasoning level. */
  reasoning: ReasoningLevel
  tags: string[]
  provenance: AgentProvenance
  /** Stable slug for system rows ("ask", "audit-stale", …); "" for user rows. */
  key: string
  source_url: string
  source_commit: string
  rev: number
  /** True when the caller's org has an active override of this system agent. */
  has_org_override: boolean
  enabled: boolean
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateAgentRequest {
  title: string
  description?: string
  prompt?: string
  produces?: ProducesKind
  default_effort?: AgentEffort
  reasoning?: ReasoningLevel
  tags?: string[]
  enabled?: boolean
}

export interface UpdateAgentRequest {
  title?: string
  description?: string
  prompt?: string
  produces?: ProducesKind
  default_effort?: AgentEffort
  reasoning?: ReasoningLevel
  tags?: string[]
  enabled?: boolean
}

export interface AgentRevisionDTO {
  uid: string
  agent_uid: string
  org_uid: string
  rev: number
  mode: 'append' | 'replace'
  body: string
  enabled: boolean
  author_uid: string
  created_at?: string | null
}

export interface SaveOverrideRequest {
  mode: 'append' | 'replace'
  body: string
  enabled?: boolean
}

export interface AgentDispatchRequest {
  repository_uid: string
  effort?: AgentEffort
  target?: Record<string, unknown>
}

/** An Agent bound to a repository with a trigger — replaces Investigation. */
export interface ScheduledAgentDTO {
  uid: string
  agent_uid: string
  repository_uid: string
  title: string
  /** "" = manual only, "on-event" = fire on push, "cron:<expr>" = scheduled. */
  trigger: string
  target: Record<string, unknown>
  /** "" = inherit the agent's default effort. */
  effort: AgentEffort | ''
  run_policy_uid?: string | null
  autonomy: Autonomy
  enabled: boolean
  provenance: 'system' | 'user'
  last_scheduled_at?: string | null
  agent_title: string
  agent_produces: ProducesKind
  agent_key: string
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateScheduledAgentRequest {
  agent_uid: string
  repository_uid: string
  title?: string
  trigger?: string
  target?: Record<string, unknown>
  effort?: AgentEffort | ''
  run_policy_uid?: string | null
  autonomy?: Autonomy
  enabled?: boolean
}

/** PATCH /scheduled-agents/{uid} — omitted fields stay unchanged. */
export interface UpdateScheduledAgentRequest {
  title?: string
  trigger?: string
  target?: Record<string, unknown>
  effort?: AgentEffort | ''
  run_policy_uid?: string | null
  autonomy?: Autonomy
  enabled?: boolean
}

/** End-of-run outcome the agent left via complete_run. */
export interface RunSummary {
  text?: string
  did?: string[]
  skipped?: string[]
  succeeded?: string[]
  failed?: string[]
  next_steps?: string[]
}

/** A run is a conversation with an agent in a workspace (PLATFORM_V3_DESIGN.md §2). */
export interface RunDTO {
  uid: string
  repository_uid: string
  playbook: RunPlaybook
  title: string
  scheduled_agent_uid: string
  /** Agent that supplied the instructions layer at dispatch ("" = code fallbacks). */
  agent_uid: string
  agent_rev: number
  executor: Executor
  execution_mode: ExecutionMode
  run_policy_uid?: string | null
  /** Resolved effort tier + reasoning level at dispatch; '' = unknown/legacy. */
  effort: string
  reasoning: string
  status: RunStatus
  linked_pr_uid: string
  linked_ticket_uid: string
  linked_finding_uid: string
  target: Record<string, unknown>
  /** '' when the workspace was destroyed/expired. */
  sandbox_uid: string
  workspace_spec: Record<string, unknown>
  cli_session_id: string
  turns: number
  usage: Record<string, unknown>
  /** LLM provider that executed this run; blank for runs predating the snapshot. */
  provider_uid?: string | null
  provider_label: string
  provider_kind: string
  provider_model: string
  /** End-of-run outcome from complete_run; {} until the agent reports. */
  summary: RunSummary
  output_refs: string[]
  raw_artifact_uri: string
  parse_status: string
  trigger: RunTrigger
  triggered_by: string
  /** Which UI owns this run: 'runs' (Runs page), 'comment' (@opensweep thread
   *  reply), 'chat' (opensweep chat bubble). */
  surface: RunSurface
  error: string
  started_at?: string | null
  completed_at?: string | null
  last_activity_at?: string | null
  ended_at?: string | null
  duration_ms: number
  created_at?: string | null
  updated_at?: string | null
}

/** Terminal takeover payload for POST /runs/{uid}/handoff. */
export interface RunHandoffDTO {
  /** resume — paste resumes the actual claude session; seeded — paste starts a
   *  fresh claude seeded by the OPENSWEEP_HANDOFF.md brief; unavailable — no
   *  live workspace, `reason` says how to recover. */
  mode: 'resume' | 'seeded' | 'unavailable'
  command: string
  sandbox_host_path: string
  cli_session_id: string
  reason: string
}

/** POST /runs — creates chat/ask runs; other playbooks have domain triggers. */
export interface CreateRunRequest {
  repository_uid: string
  playbook: RunPlaybook
  prompt?: string
  title?: string
  target?: Record<string, unknown>
  linked_pr_uid?: string
  linked_ticket_uid?: string
  linked_finding_uid?: string
  /** 'chat' marks a opensweep chat-bubble session (hidden from the Runs page). */
  surface?: RunSurface
  /** What the user was viewing when the chat started. */
  context?: { subject_type?: string; subject_uid?: string }
}

/** Reply of the blocking REST follow-up POST /runs/{uid}/messages. */
export interface RunMessageResult {
  content: string
  status: RunStatus
  interrupted: boolean
  error: string
}

export interface ArtifactDTO {
  uri: string
  text: string
  bytes: number
}

// ── ActiveRuns (in-flight run awareness on dispatch surfaces) ───────────────

export type ActiveRunStatus = 'queued' | 'running' | 'paused_quota'

/** Row of GET /runs/active — one per in-flight run on a subject. */
export interface ActiveRunDTO {
  run_uid: string
  scheduled_agent_uid: string
  title: string
  playbook: string
  status: ActiveRunStatus
  started_at?: string | null
  repository_uid: string
}

export interface ActiveRunFilters {
  repository_uid?: string
  pull_request_uid?: string
  ticket_uid?: string
  finding_uid?: string
  playbook?: string
}

/** One structured transcript event (PLATFORM_V3_DESIGN.md §4). The server
 *  parses executor output into these; the UI renders events, never raw stdout. */
export type RunTranscriptEventType =
  | 'user_message'
  | 'assistant_text'
  | 'tool_use'
  | 'tool_result'
  | 'system'
  | 'turn_end'
  | 'error'
  | 'narration'

export interface RunTranscriptEvent {
  seq: number
  ts: string
  turn: number
  type: RunTranscriptEventType
  /** user_message / assistant_text / system */
  text?: string
  /** tool_use / tool_result */
  name?: string
  input?: string
  output?: string
  is_error?: boolean
  /** system marker kind: sandbox | run_status | … */
  kind?: string
  /** narration: seq of the tool_use this line describes */
  covers_seq?: number
  /** turn_end */
  status?: string
  usage?: Record<string, unknown>
  /** error */
  detail?: string
}

/** Chunk of GET /runs/{uid}/transcript?after_seq=N — poll with
 *  the returned last_seq; `done: true` means the run stopped writing. */
export interface RunTranscriptDTO {
  events: RunTranscriptEvent[]
  last_seq: number
  done: boolean
}

// ── Run workspace changes (Files tab) ────────────────────────────────────────

export type RunChangeStatus = 'added' | 'modified' | 'deleted' | 'renamed'

/** One file the agent touched during the run, with its unified diff. */
export interface RunChangedFile {
  path: string
  old_path: string
  status: RunChangeStatus
  additions: number
  deletions: number
  patch: string
  binary: boolean
  too_large: boolean
}

/** GET /runs/{uid}/changes — live workspace diff or the end-of-run snapshot. */
export interface RunChangesDTO {
  source: 'live' | 'snapshot' | 'none'
  base: string
  captured_at?: string | null
  files: RunChangedFile[]
  tree: string[]
}

/** Object-shaped 409 detail on dispatch endpoints when a same-target run is
 *  already in flight (detail may still be a plain string for other 409s). */
export interface DispatchConflictDetail {
  message: string
  run_uid: string
  scheduled_agent_uid: string
}

/** Shape of `usage.quota` on runs paused by a provider quota/rate limit. */
export interface RunQuotaUsage {
  retry_count?: number
  next_retry_at?: string | null
  detected_at?: string | null
  provider_uid?: string
  exhausted_provider_uids?: string[]
  fallback_available?: boolean
  [key: string]: unknown
}


// ── Docs (curated markdown pages; agents propose edits as DocEdits) ─────────

export interface DocDTO {
  uid: string
  repository_uid: string
  /** Path-like stable id, unique per repository: "conventions", "backend/queue-workers".
   *  "/" segments form folders in the wiki tree. */
  slug: string
  title: string
  summary: string
  body: string
  /** Pinned pages are injected verbatim into every run's first prompt. */
  pinned: boolean
  /** Code paths this page documents — staleness is derived from them. */
  watch_paths: string[]
  /** True when watched code changed after the page was last reviewed. */
  stale: boolean
  /** The watched paths that changed since the last review. */
  stale_paths: string[]
  code_changed_at?: string | null
  last_reviewed_at?: string | null
  /** Count of pending DocEdits against this page. */
  pending_edits: number
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateDocRequest {
  repository_uid: string
  slug: string
  title?: string
  summary?: string
  body?: string
  watch_paths?: string[]
  pinned?: boolean
}

export interface UpdateDocRequest {
  title?: string
  summary?: string
  body?: string
  watch_paths?: string[]
}

export type DocEditStatus = 'pending' | 'accepted' | 'rejected'

export interface DocEditDTO {
  uid: string
  repository_uid: string
  /** "" = proposes a NEW page. */
  doc_uid: string
  slug: string
  title: string
  summary: string
  watch_paths: string[]
  /** Full replacement markdown — the UI renders the diff against current_body. */
  proposed_body: string
  rationale: string
  source_run_uid: string
  status: DocEditStatus
  resolved_by: string
  resolved_at?: string | null
  created_at?: string | null
  /** Body of the target Doc at read time ("" for new-page proposals). */
  current_body: string
}

// ── Memory (flat agent-written notes; staleness computed at read time) ──────

export interface MemoryDTO {
  uid: string
  repository_uid: string
  /** Optional uid of the entity the fact is about. */
  anchor_uid: string
  title: string
  body: string
  source_run_uid: string
  /** Computed: anchored code changed after the memory was last updated. */
  possibly_stale: boolean
  created_at?: string | null
  updated_at?: string | null
}

// ── Freshness (Checked stamps, derived per scope) ────────────────────────────

export type CheckedOutcome = 'clean' | 'findings' | 'failed'

/** Row of GET /repositories/{uid}/freshness — one per Doc (scope_uid = doc uid,
 *  or the repository uid for the repo-level stamp). */
export interface ScopeFreshnessDTO {
  scope_uid: string
  last_checked: string | null
  revision: string
  outcome: CheckedOutcome
  code_changed_since: boolean
}

// ── RunPolicy ───────────────────────────────────────────────────────────────

export interface RunPolicyDTO {
  uid: string
  name: string
  description: string
  max_wall_seconds?: number | null
  max_tool_turns?: number | null
  max_files_touched?: number | null
  /** null = unbounded continuation passes (wall-limited only). */
  max_continuation_passes?: number | null
  cloud_allowed: boolean
  local_only: boolean
  allowed_executors: string[]
  dry_run: boolean
  warn_at_pct: number
  daily_repo_run_count?: number | null
  daily_repo_wall_seconds?: number | null
  daily_repo_dollars?: number | null
  version: number
  supersedes_uid?: string | null
  created_at?: string | null
  updated_at?: string | null
}

// ── Metrics ─────────────────────────────────────────────────────────────────

export interface FindingStatusCount { status: string; count: number }
export interface RepoSummary {
  repository_uid: string
  repository_name: string
  repository_slug: string
  docs: number
  open_findings: number
  high_severity_findings: number
  proposals: number
  runs_last_24h: number
}
export interface OverviewMetrics {
  repositories_github: number
  total_docs: number
  open_findings: number
  high_severity_findings: number
  proposals: number
  runs_last_24h: number
  finding_statuses: FindingStatusCount[]
  repositories: RepoSummary[]
}

// ── User ────────────────────────────────────────────────────────────────────

export interface UserDTO {
  uid: string
  email: string
  display_name: string
  role?: string
}

export interface MeDTO {
  uid: string
  email: string
  display_name: string
  role: 'viewer' | 'maintainer' | 'admin'
  org_uid: string
  org_role: 'owner' | 'member'
  is_platform_admin: boolean
  onboarded: boolean
  created_at?: string | null
}

// ── Organization (multi-tenancy) ────────────────────────────────────────────

export type RefineFalsePositivePolicy = 'annotate' | 'dismiss' | 'wont-fix'

export interface OrgSettingsDTO {
  refine_false_positive_policy: RefineFalsePositivePolicy
}

export interface OrganizationDTO {
  uid: string
  name: string
  created_at?: string | null
  member_count: number
  repository_count: number
  is_owner: boolean
  settings: OrgSettingsDTO
}

export interface OrgMemberDTO {
  uid: string
  email: string
  display_name: string
  role: 'viewer' | 'maintainer' | 'admin'
  org_role: 'owner' | 'member'
  created_at?: string | null
}

export interface OrgInvitationDTO {
  uid: string
  email: string
  role: 'viewer' | 'maintainer' | 'admin'
  status: string
  invited_by: string
  created_at?: string | null
}

/** Invitation addressed to *me* (surfaced on /me/profile). */
export interface MyInvitationDTO {
  uid: string
  org_uid: string
  org_name: string
  role: 'viewer' | 'maintainer' | 'admin'
  created_at?: string | null
}

export interface MeProfileDTO {
  uid: string
  email: string
  display_name: string
  role: 'viewer' | 'maintainer' | 'admin'
  org_role: 'owner' | 'member'
  is_platform_admin: boolean
  onboarded: boolean
  org: OrganizationDTO
  pending_invitations: MyInvitationDTO[]
}

// ── Admin: Sandbox (run workspaces) ─────────────────────────────────────────

export interface Sandbox {
  uid: string
  repository_uid: string
  host_path: string
  container_path: string
  source_branch: string
  sandbox_branch: string
  /** discovery | write */
  purpose?: string
  status: string
  created_at?: string | null
  destroyed_at?: string | null
  cleanup_after?: string | null
  error?: string
}

// ── LLM provider (admin) ────────────────────────────────────────────────────

export type LLMProviderKind =
  | 'claude_subscription'
  | 'codex_subscription'
  | 'claude_api'
  | 'openai_api'
  | 'mlx'
  | 'lmstudio'
  | 'ollama'
  | 'opencode'
  | 'aider'
  | 'custom'

export type LLMProviderHealth = 'ok' | 'degraded' | 'unreachable' | 'unknown'

export interface LLMProvider {
  uid: string
  label: string
  kind: LLMProviderKind
  base_url?: string
  model?: string
  api_key_env?: string
  cli_command_template?: string
  extra_args?: string
  enabled: boolean
  active: boolean
  /** Owning org uid. */
  org_uid: string
  notes?: string
  has_credential_secret?: boolean
  last_health_check_at?: string | null
  last_health_status?: LLMProviderHealth
  last_health_detail?: string
  created_at?: string | null
  updated_at?: string | null
}

/** Shape returned by `GET /api/v1/llm-providers/status` — org provider readiness. */
export interface LLMProviderStatus {
  configured: boolean
  provider_count: number
  active_uid: string
  active_label: string
}

/** Shape returned by `GET /api/v1/llm-providers/catalog` — one entry per kind. */
export interface LLMProviderKindMeta {
  kind: LLMProviderKind
  display_name: string
  transport: string
  default_cli?: string
  needs_api_key?: boolean
  needs_base_url?: boolean
  needs_credential?: boolean
  credential_label?: string
  credential_placeholder?: string
  setup_steps?: string[]
  default_model?: string
  /** Connect-dialog tile metadata (platform-owned). */
  default_label?: string
  tagline?: string
  default_base_url?: string
  default_api_key_env?: string
  /** Picker order; 0/absent = hidden from the picker (ops-only kinds). */
  featured?: number
}

// ── Audit (Event) ───────────────────────────────────────────────────────────

export interface AuditEvent {
  uid: string
  kind: string
  subject_uid?: string | null
  subject_type?: string | null
  actor_uid?: string | null
  payload: Record<string, unknown>
  occurred_at: string
}

// ── Notifications (inbox / attention centre) ────────────────────────────────

export type NotificationCategory = 'attention' | 'activity' | 'mentions'

export interface NotificationDTO {
  uid: string
  kind: string
  category: NotificationCategory
  label: string
  title: string
  subject_type: string
  subject_uid: string
  repository_uid: string
  payload: Record<string, unknown>
  occurred_at: string | null
  read_at?: string | null
}

export interface NotificationCountsDTO {
  total: number
  attention: number
  activity: number
  mentions: number
}

// ── Delivery (PR convergence ledger) ────────────────────────────────────────

export type PRState = 'open' | 'closed' | 'merged'
export type CIState = 'green' | 'red' | 'pending' | 'empty'
export type VerdictResult = 'approve' | 'request_changes' | 'needs_human'
export type ResolutionState =
  | 'open'
  | 'in-fix'
  | 'fixed'
  | 'verified'
  | 'reopened'
  | 'deferred'
  | 'waived'
  | 'refuted'
export type BlockingOverrideValue = '' | 'block' | 'allow'
/** Skeptic pass state on a Verdict: '' = never verified. */
export type VerdictVerificationStatus = '' | 'pending' | 'adjusted' | 'superseded' | 'failed'
export type ReviewDepth = 'quick' | 'normal' | 'deep'

export interface CICheck {
  name: string
  status?: string | null
  conclusion?: string | null
  url?: string | null
}

export interface ConvergenceCounts {
  blocking: number
  deferred: number
  waived: number
  info: number
}

/** Output of the convergence predicate (PLATFORM_V2_DESIGN.md §5). */
export interface ConvergenceState {
  converged: boolean
  head_sha: string
  ci_state: CIState
  verdict_fresh: boolean
  verdict_result: VerdictResult | null
  verdict_sha: string
  clean_round: boolean
  counts: ConvergenceCounts
  reasons: string[]
}

export interface PullRequestDTO {
  uid: string
  repository_uid: string
  github_number: number
  title: string
  author: string
  url: string
  state: PRState
  draft: boolean
  head_sha: string
  head_ref: string
  base_ref: string
  base_is_default: boolean
  ticket_uid: string
  ci_state: CIState
  ci_checks: CICheck[]
  fix_rounds: number
  /** True once the fix-round bound (MergePolicy.max_fix_rounds) is spent — human takes over. */
  fix_rounds_exhausted?: boolean
  /** Pending waiver requests on this PR's ledger (from the list DTO). */
  waive_requested_count?: number
  converged: boolean
  convergence: ConvergenceState | null
  created_at?: string | null
  updated_at?: string | null
  last_synced_at?: string | null
}

export interface ACResult {
  criterion: string
  result: string // pass | fail | unverifiable
  note: string
}

export interface VerdictDTO {
  uid: string
  pull_request_uid: string
  repository_uid: string
  sha: string
  result: VerdictResult
  new_blocking_findings: number
  finding_uids: string[]
  ac_results: ACResult[]
  source_run_uid: string
  executor: string
  verification_status: VerdictVerificationStatus
  verification_run_uid: string
  created_at?: string | null
}

export interface FindingResolutionDTO {
  uid: string
  finding_uid: string
  pull_request_uid: string
  repository_uid: string
  introduced_at_sha: string
  state: ResolutionState
  fixed_at_sha: string
  verified_at_sha: string
  verified_by_run_uid: string
  waived_by: string
  waive_reason: string
  waive_requested_by: string
  waive_requested_reason: string
  blocking_override: BlockingOverrideValue
  blocking_override_reason: string
  ticket_uid: string
  /** Computed against the repo MergePolicy — never stored on the finding. */
  blocking: boolean
  created_at?: string | null
  updated_at?: string | null
  // denormalized finding facets for triage views
  finding_title: string
  finding_severity: Severity
  finding_tags: string[]
}

export interface MergePolicyDTO {
  uid: string
  repository_uid: string
  blocking: Record<string, unknown>
  require_clean_round: boolean
  max_fix_rounds: number
  /** Regex strings — paths the write path must never touch. */
  path_denylist: string[]
}

export interface UpdateMergePolicyRequest {
  blocking?: Record<string, unknown>
  require_clean_round?: boolean
  max_fix_rounds?: number
  path_denylist?: string[]
}

// ── Per-repository workflow config (stage → prompt + auto toggles) ──────────

export type WorkflowStage = 'ask' | 'analysis' | 'discover' | 'review' | 'fix' | 'implement' | 'verify' | 'document'

export interface WorkflowStageConfig {
  /** Empty string → the built-in intent for this stage (no extra prompt). */
  agent_uid: string
  /** Only honored for stages listed in WorkflowConfig.auto_stages. */
  auto: boolean
  /** Recall/precision dial applied by stage triggers (auto reviews read review.depth). */
  depth: ReviewDepth
  /** Pin this stage's runs to a specific LLM provider. Empty → active provider chain. */
  provider_uid: string
  /** Model override for this stage's runs. Empty → the provider's own model. */
  model: string
  /** Wall-clock ceiling for this stage's runs. 0 → the run policy's ceiling. */
  max_wall_seconds: number
  /** Full run-policy override for this stage (dollars/wall/turns/files). Empty
   * → the system default. */
  run_policy_uid: string
}

/** GET/PUT /repositories/{uid}/workflow — all stages are always present. */
export interface WorkflowConfig {
  stages: Record<WorkflowStage, WorkflowStageConfig>
  auto_stages: WorkflowStage[]
}

export interface UpdateWorkflowRequest {
  stages: Record<WorkflowStage, WorkflowStageConfig>
}

// ── Per-repository static-analyzer config ───────────────────────────────────

export type AnalyzerMode = 'auto' | 'custom' | 'off'

export interface AnalyzerTool {
  tool: string
  args: string[]
  paths: string[]
}

/** GET/PUT /repositories/{uid}/analyzers. */
export interface AnalyzersConfig {
  mode: AnalyzerMode
  tools: AnalyzerTool[]
}

/** Body of POST /delivery/pull-requests/{uid}/verdicts. */
export interface SubmitVerdictRequest {
  sha: string
  result: VerdictResult
  new_blocking_findings?: number
  finding_uids?: string[]
  ac_results?: ACResult[]
  source_run_uid?: string
  executor?: string
}

/** Body of POST /delivery/pull-requests/{uid}/review. */
export interface TriggerReviewRequest {
  depth?: ReviewDepth
  /** Force a full base...head review instead of an incremental one. */
  full?: boolean
  /** Numeric budget: caps normal/deep, overrides quick's default of 5. */
  max_findings?: number | null
}

/** Response of POST /delivery/pull-requests/{uid}/review. */
export interface ReviewRunDispatch {
  run_uid: string
  scheduled_agent_uid: string
  head_sha: string
  depth?: string
  /** Non-empty = incremental review scoped from this prior-verdict sha. */
  incremental_from?: string
}

/** Response of POST /tickets/{uid}/implement — inspect loosely. */
export type ImplementRunDispatch = { run_uid?: string; scheduled_agent_uid?: string } & Record<string, unknown>

/** Response of POST /delivery/pull-requests/{uid}/fix — inspect loosely. */
export type FixRunDispatch = {
  run_uid?: string
  scheduled_agent_uid?: string
  fix_round?: number
} & Record<string, unknown>

export interface TriggerFixRequest {
  finding_uids?: string[]
}

export interface RatchetRequest {
  repository_uid: string
  /** The finding class is a (tag, subtype) pair — both are required. */
  tag: string
  subtype: string
}

/** Response of POST /findings/ratchet — inspect loosely. */
export type RatchetDispatch = {
  ticket_uid?: string
  run_uid?: string
  finding_count?: number
} & Record<string, unknown>

// ── Tickets (delivery work items, PLATFORM_V2_DESIGN.md §2/§12) ─────────────

export type TicketStatus = 'backlog' | 'todo' | 'in-progress' | 'in-review' | 'done'
export type TicketPriority = 'low' | 'medium' | 'high' | 'urgent'
export type TicketSize = '' | 'trivial' | 'small' | 'medium' | 'large'
export type TicketOrigin = 'finding' | 'human' | 'agent-proposal'

/** Thread-authored implementation plan, mirrored onto the ticket as
 *  metadata (unified dev flow). Empty object = no plan yet. */
export interface TicketPlan {
  markdown?: string
  state?: 'drafted' | 'approved' | 'none'
  thread_uid?: string
  updated_at?: string | null
  approved_by?: string
  approved_at?: string | null
}

export interface TicketDTO {
  uid: string
  repository_uid: string
  title: string
  description: string
  acceptance_criteria: string[]
  labels: string[]
  status: TicketStatus
  priority: TicketPriority
  size: TicketSize
  origin: TicketOrigin
  origin_finding_uid: string
  parent_ticket_uid: string
  linked_finding_uids: string[]
  linked_pr_uids: string[]
  assignee_uid: string
  plan: TicketPlan
  approved_by: string
  approved_at?: string | null
  done_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

/** Response of GET /tickets/{uid} — the ticket itself (flat) plus its subtickets. */
export interface TicketDetailDTO extends TicketDTO {
  children: TicketDTO[]
}

export interface CreateTicketRequest {
  repository_uid: string
  title: string
  description?: string
  acceptance_criteria?: string[]
  labels?: string[]
  priority?: TicketPriority
  size?: TicketSize
  origin?: TicketOrigin
  origin_finding_uid?: string
  parent_ticket_uid?: string
}

export interface UpdateTicketRequest {
  title?: string
  description?: string
  acceptance_criteria?: string[]
  labels?: string[]
  priority?: TicketPriority
  size?: TicketSize
  parent_ticket_uid?: string
}

// ── Ticket group proposals (agent-suggested batches, human-approved) ────────

export type GroupProposalStatus = 'proposed' | 'approved' | 'rejected'

export interface TicketGroupProposalDTO {
  uid: string
  repository_uid: string
  title: string
  rationale: string
  member_ticket_uids: string[]
  suggested_labels: string[]
  suggested_priority: TicketPriority
  status: GroupProposalStatus
  source_run_uid: string
  /** Parent ticket materialized by approval (empty until then). */
  created_ticket_uid: string
  reviewed_by: string
  reviewed_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

/** POST /tickets/group — batch ≥2 tickets under a new parent ticket. */
export interface GroupTicketsRequest {
  repository_uid: string
  title: string
  description?: string
  member_ticket_uids: string[]
  labels?: string[]
  priority?: TicketPriority
}

/** Response of POST /tickets/propose-groups — inspect loosely. */
export type ProposeGroupsDispatch = {
  run_uid?: string
  scheduled_agent_uid?: string
  candidate_count?: number
} & Record<string, unknown>

// ── Comments (discussion threads on any data item) ──────────────────────────

export type CommentSubjectType =
  | 'finding'
  | 'ticket'
  | 'pull_request'
  | 'news_item'
  | 'run'
  | 'scheduled_agent'
  | 'doc'

export type CommentAuthorKind = 'user' | 'opensweep'

/** One `@[Label](type:uid)` token parsed out of a comment body. */
export interface MentionRef {
  type: string
  uid: string
  label: string
}

export interface CommentDTO {
  uid: string
  subject_type: CommentSubjectType
  subject_uid: string
  author_uid: string
  author_name: string
  author_kind: CommentAuthorKind
  source_run_uid: string
  body: string
  mentions: MentionRef[]
  /** Reply threading (one level): uid of the parent comment, '' = top-level. */
  parent_comment_uid: string
  /** Machine metadata for platform-authored comments — thread-question
   *  mirrors carry {kind: 'thread_question', thread_uid, question_uid,
   *  options, status}. */
  meta: Record<string, unknown>
  /** Set when the comment summoned @opensweep and a background run was dispatched. */
  triggered_run_uid: string
  created_at: string
}

export interface CreateCommentRequest {
  subject_type: CommentSubjectType
  subject_uid: string
  body: string
  /** Reply threading: uid of the comment being replied to. */
  parent_comment_uid?: string
}

/** An in-flight @opensweep reply run for a thread — drives the thinking bubble. */
export interface PendingOpenSweepRunDTO {
  run_uid: string
  comment_uid: string
  status: string
  started_at?: string | null
}

/** Types addressable from the @-mention dropdown (comment subjects + group). */
export type MentionTargetType = CommentSubjectType | 'group'

export interface MentionSearchResult {
  type: MentionTargetType
  uid: string
  label: string
  sublabel: string
  repository_uid: string
}

// ── Slack integration (workspace connection + notification rules) ───────────

/** Notification event the backend can route to Slack (catalog on /slack/status). */
export interface SlackEventTypeDTO {
  event_type: string
  label: string
  description: string
}

export interface SlackStatusDTO {
  /** Platform operator has set SLACK_CLIENT_ID/SECRET — install is possible. */
  configured: boolean
  /** This org has an installed Slack workspace connection. */
  connected: boolean
  team_id: string
  team_name: string
  bot_user_id: string
  scopes: string[]
  installed_by: string
  event_types: SlackEventTypeDTO[]
}

export interface SlackChannelDTO {
  id: string
  name: string
  is_private: boolean
}

export interface SlackRuleDTO {
  uid: string
  event_type: string
  channel_id: string
  channel_name: string
  /** '' = all repositories. */
  repository_uid: string
  enabled: boolean
  created_by: string
}

export interface SlackRuleCreateRequest {
  event_type: string
  channel_id: string
  channel_name: string
  /** '' = all repositories. */
  repository_uid: string
  enabled: boolean
}

export type SlackRuleUpdateRequest = Partial<SlackRuleCreateRequest>

// ── Threads (unified dev flow) ──────────────────────────────────────────────

export type ThreadPhase = 'refining' | 'implementing' | 'in_review' | 'done' | 'abandoned'
export type PlanState = 'none' | 'drafted' | 'approved'

export interface ThreadDTO {
  uid: string
  repository_uid: string
  subject_ticket_uid: string
  phase: ThreadPhase
  plan_state: PlanState
  branch: string
  pr_uid: string
  ready_for_review: boolean
  active_run_uid: string
  created_by: string
  created_at: string | null
  updated_at: string | null
}

export interface ThreadRunSummaryDTO {
  uid: string
  playbook: string
  status: string
  title: string
  created_at: string | null
}

export interface ThreadEventDTO {
  ts: string
  type: string
  [key: string]: unknown
}

export interface ThreadDetailDTO extends ThreadDTO {
  plan_text: string
  progress: ThreadProgress
  events: ThreadEventDTO[]
  runs: ThreadRunSummaryDTO[]
}

/** Derived thread progress — computed by the platform from observed facts
 *  (questions, plan, PR, verdicts, fix rounds); never stored. */
export interface ThreadProgress {
  phase: ThreadPhase
  label: string
  questions_total: number
  questions_answered: number
  questions_open: number
  plan_state: PlanState
  pr_opened: boolean
  fix_rounds: number
  last_verdict: string
}

// ── Campaigns (partitioned audit sweeps) ────────────────────────────────────

export type CampaignStatus = 'planning' | 'running' | 'finalizing' | 'done' | 'failed' | 'cancelled'
export type CampaignTemplate = 'full' | 'rotation' | 'focused'
export type CampaignPartKind = 'area' | 'feature' | 'global'
export type CampaignPartState = 'pending' | 'running' | 'done' | 'failed'

/** One bounded slice of a campaign — an area sweep or a whole-repo pass. */
export interface CampaignPart {
  idx: number
  kind: CampaignPartKind
  title: string
  /** Area-map keys bundled into this part (empty for global sweeps). */
  area_keys: string[]
  scope_paths: string[]
  doc_uids: string[]
  lens_keys: string[]
  /** '' until the part's run is dispatched. */
  run_uid: string
  state: CampaignPartState
  file_count: number | null
}

export interface CampaignCoveragePart {
  idx: number
  title: string
  covered: number
  skipped: number
  state: CampaignPartState
}

/** End-of-campaign digest; {} until finalization. */
export interface CampaignSummary {
  counts?: {
    by_severity: Record<string, number>
    by_part: Record<string, number>
    total: number
  }
  coverage?: {
    parts: CampaignCoveragePart[]
    /** Scope paths of failed/never-run parts — the coverage debt left behind. */
    holes: string[]
  }
  failed_parts?: number[]
}

/** Append-only lifecycle log entry (created/launched/part_done/finalized/…). */
export interface CampaignEvent {
  ts: string
  type: string
  [key: string]: unknown
}

export interface CampaignDTO {
  uid: string
  repository_uid: string
  title: string
  status: CampaignStatus
  template: CampaignTemplate
  /** '' = default tiers (areas normal, global sweeps deep). */
  effort: AgentEffort | ''
  lens_keys: string[]
  /** Rotation only: how many areas each pass covers. */
  k: number
  /** '' = the whole map; else the sweep is scoped to areas under this key prefix. */
  area_prefix: string
  parts: CampaignPart[]
  max_parallel: number
  created_by: string
  trigger_provenance: string
  summary: CampaignSummary
  events: CampaignEvent[]
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateCampaignRequest {
  template: CampaignTemplate
  /** Empty = every enabled lens; 'focused' reads its focus lens from [0]. */
  lens_keys?: string[]
  effort?: AgentEffort | ''
  /** Rotation only: how many areas this pass covers. */
  k?: number
  max_parallel?: number
  title?: string
  /** Scope the sweep to areas under this key prefix ('' = the whole map). */
  area_prefix?: string
}

/** One area of the would-be partition (campaign-areas preview). */
export interface CampaignAreaPreview {
  /** The area-map key ('' for docs-derived partitions). */
  area_key: string
  title: string
  scope_paths: string[]
  /** Scope paths that match no files in the current tree. */
  dead_scope_paths: string[]
  doc_uids: string[]
  /** null when the file tree was unavailable (degraded sizing). */
  file_count: number | null
}

/** The partition a campaign would use right now — computed live, never persisted. */
export interface CampaignAreasPreview {
  areas: CampaignAreaPreview[]
  /** Where the partition comes from ('area-map' or docs-derived). */
  source: string
  /** '' = planned against the full tree; else why sizing degraded. */
  degraded: string
  total_files: number
  /** Files no partition leaf covers (the map-drift remainder). */
  uncovered_files: number
  /** Files claimed by more than one partition leaf. */
  overlapping_files: number
  /** Area keys whose file count exceeds the target part size. */
  oversized_areas: string[]
  /** Ignore-area scope paths that match nothing in the tree. */
  dead_ignore_scopes: string[]
}

// ── Lenses (audit checklist prompts — platform rows, org-tunable) ───────────

export type LensScope = 'local' | 'global'

export interface LensDTO {
  uid: string
  key: string
  title: string
  scope: LensScope
  body: string
  tags: string[]
  wants: string[]
  /** Global lenses only: the sweep agent this lens backs. */
  global_agent_key: string
  enabled: boolean
  provenance: string
  created_at?: string | null
  updated_at?: string | null
}

/** PATCH /lenses/{key} — structure (key/scope/wants) stays platform-owned;
 *  the tunable surface is the prose and its labels. */
export interface UpdateLensRequest {
  title?: string
  body?: string
  tags?: string[]
  enabled?: boolean
}

// ── Legacy short-name aliases ───────────────────────────────────────────────
// Components written before the *DTO rename import these. Cheaper to keep the
// aliases than touch every component.

export type Repository = RepositoryDTO
export type FileContent = FileContentDTO

// ── Areas (the reviewed audit partition — the Area map) ─────────────────────
// Humans edit Areas directly; agents propose AreaEdits that are accepted or
// rejected on the Areas view. Keys are path-like ("backend/delivery"); the
// hierarchy is derived from key prefixes, never stored.

export type AreaKind = 'subsystem' | 'feature' | 'ignore'
export type AreaEditStatus = 'pending' | 'accepted' | 'rejected'

export interface AreaDTO {
  uid: string
  repository_uid: string
  key: string
  kind: AreaKind
  title: string
  scope_paths: string[]
  spec: string
  doc_uids: string[]
  enabled: boolean
  provenance: string
  /** Derived: code changed under scope_paths since last review. */
  stale: boolean
  stale_paths: string[]
  code_changed_at?: string | null
  last_reviewed_at?: string | null
  pending_edits: number
  created_at?: string | null
  updated_at?: string | null
}

/** Agent-proposed full replacement for an area (or a new area when area_uid=''). */
export interface AreaEditDTO {
  uid: string
  repository_uid: string
  area_uid: string
  key: string
  kind: string
  title: string
  scope_paths: string[]
  doc_uids: string[]
  proposed_spec: string
  /** false = the edit proposes retiring the area. */
  proposed_enabled: boolean
  rationale: string
  source_run_uid: string
  status: AreaEditStatus
  resolved_by: string
  resolved_at?: string | null
  created_at?: string | null
  /** Current spec of the target area (empty for new-area proposals) — lets
   *  the UI hint current vs proposed without a second fetch. */
  current_spec: string
}

export interface UpdateAreaRequest {
  title?: string
  kind?: AreaKind
  scope_paths?: string[]
  spec?: string
  doc_uids?: string[]
  enabled?: boolean
}

/** PATCH /areas/{uid} — applies the edit and returns partition warnings to eyeball. */
export interface UpdateAreaResponse {
  area: AreaDTO
  warnings: string[]
}

/** One scope path of an area, sized against the current file tree. */
export interface AreaScopeEntry {
  path: string
  /** null when the file tree was unavailable. */
  file_count: number | null
  /** true = the path matches nothing in the tree. */
  dead: boolean
  /** Capped sample of matched files. */
  files: string[]
}

/** A doc page reachable from an area (linked or suggested). */
export interface AreaDocLink {
  uid: string
  slug: string
  title: string
}

/** Another area adjacent to this one (shared paths / docs / parent-child). */
export interface AreaRelated {
  uid: string
  key: string
  kind: AreaKind
  title: string
}

/** One campaign-part coverage stamp touching this area. */
export interface AreaCoverageStamp {
  run_uid: string
  outcome: string
  checked_at: string | null
  lens_verdicts: { lens: string; verdict: string; note?: string }[]
}

/** GET /areas/{uid}/detail — everything the area detail page renders. */
export interface AreaDetailDTO {
  area: AreaDTO
  scope: AreaScopeEntry[]
  /** '' = scope sized against the full tree; else why sizing degraded. */
  tree_degraded: string
  linked_docs: AreaDocLink[]
  suggested_docs: AreaDocLink[]
  related_areas: AreaRelated[]
  coverage: AreaCoverageStamp[]
  pending_edits: AreaEditDTO[]
}

/** Accepting an edit applies it and returns partition warnings to eyeball. */
export interface AcceptAreaEditResponse {
  area: AreaDTO
  warnings: string[]
}

/** Response of POST /repositories/{uid}/sweep/map-areas (409 when already running). */
export interface MapAreasResponse {
  repository_uid: string
  run_uid: string
  errors: string[]
  summary: string
}
