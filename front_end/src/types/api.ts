// Generated against PLATFORM.md backend (v0.3.0).
// Core primitives: Investigation, Finding, Doc/Memory — plus RunPolicy and the
// platform tool surface.

// ── Enums ───────────────────────────────────────────────────────────────────

export type RepositoryMode = 'github'

export type FindingKind = 'defect' | 'improvement' | 'gap' | 'proposal' | 'observation' | 'feature-idea'
export type Severity = 'low' | 'medium' | 'high' | 'critical'
export type Effort = 'trivial' | 'small' | 'medium' | 'large'
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
export type ComputeDial = 'disabled' | 'suggest' | 'ask-before-run' | 'auto-run-cheap' | 'auto-run-any'
export type InvestigationProvenance = 'human-asked' | 'llm-proposed' | 'template'
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
export type RunPlaybook = 'chat' | 'ask' | 'review' | 'fix' | 'implement' | 'verify' | 'document'
export type RunTrigger = 'manual' | 'event' | 'schedule'
export type RunSurface = 'runs' | 'comment' | 'chat'
export type InvestigationEffort = 'quick' | 'normal' | 'deep'

export type OnExceed = 'abort' | 'pause_for_approval'

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
  effort: Effort
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
  effort?: Effort
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
  effort?: Effort
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

// ── Investigation + Run ─────────────────────────────────────────────────────

export interface InvestigationDTO {
  uid: string
  repository_uid: string
  title: string
  description: string
  intent: string
  job_type: string
  target: Record<string, unknown>
  effort: InvestigationEffort
  schedule: string
  default_executor: Executor
  default_mode: ExecutionMode
  run_policy_uid?: string | null
  provenance: InvestigationProvenance
  compute_dial: ComputeDial
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateInvestigationRequest {
  repository_uid: string
  title?: string
  description?: string
  intent?: string
  job_type?: string
  target?: Record<string, unknown>
  effort?: InvestigationEffort
  schedule?: string
  default_executor?: Executor
  default_mode?: ExecutionMode
  run_policy_uid?: string | null
  compute_dial?: ComputeDial
}

/** PATCH /investigations/{uid} — omitted fields stay unchanged. */
export interface UpdateInvestigationRequest {
  title?: string
  description?: string
  intent?: string
  target?: Record<string, unknown>
  effort?: InvestigationEffort
  /** "" = manual only, "on-event" = fire on push, "cron:<expr>" = scheduled. */
  schedule?: string
  compute_dial?: ComputeDial
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
  investigation_uid: string
  executor: Executor
  execution_mode: ExecutionMode
  run_policy_uid?: string | null
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
  investigation_uid: string
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
  investigation_uid: string
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

export interface InvestigationJobTypeDTO {
  job_type: string
  title: string
  description: string
  intent: string
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
  max_tokens?: number | null
  max_dollars?: number | null
  max_wall_seconds?: number | null
  max_tool_turns?: number | null
  max_files_touched?: number | null
  max_test_seconds?: number | null
  cloud_allowed: boolean
  local_only: boolean
  allowed_executors: string[]
  dry_run: boolean
  warn_at_pct: number
  on_exceed: OnExceed
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
  agent_prompt_uid: string
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
   * → the investigation pin, then the system default. */
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
  investigation_uid: string
  head_sha: string
  depth?: string
  /** Non-empty = incremental review scoped from this prior-verdict sha. */
  incremental_from?: string
}

/** Response of POST /tickets/{uid}/implement — inspect loosely. */
export type ImplementRunDispatch = { run_uid?: string; investigation_uid?: string } & Record<string, unknown>

/** Response of POST /delivery/pull-requests/{uid}/fix — inspect loosely. */
export type FixRunDispatch = {
  run_uid?: string
  investigation_uid?: string
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
  investigation_uid?: string
  candidate_count?: number
} & Record<string, unknown>

// ── Comments (discussion threads on any data item) ──────────────────────────

export type CommentSubjectType =
  | 'finding'
  | 'ticket'
  | 'pull_request'
  | 'news_item'
  | 'run'
  | 'investigation'
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
  /** Native executor todos mirrored per phase (durable, updated per turn). */
  todos: Record<string, AgentTodo[]>
  events: ThreadEventDTO[]
  runs: ThreadRunSummaryDTO[]
}

/** Agent working-plan item, mirrored live from Claude Code's TodoWrite calls
 *  in the transcript (unified dev flow). */
export interface AgentTodo {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
  activeForm?: string
}

// ── Legacy short-name aliases ───────────────────────────────────────────────
// Components written before the *DTO rename import these. Cheaper to keep the
// aliases than touch every component.

export type Repository = RepositoryDTO
export type FileContent = FileContentDTO
