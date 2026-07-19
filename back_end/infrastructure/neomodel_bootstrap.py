"""Declare Neo4j constraints + indexes at startup."""

from neomodel import adb

from logging_config import logger

_CONSTRAINTS = [
    # Core
    "CREATE CONSTRAINT user_uid IF NOT EXISTS FOR (n:User) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT org_uid IF NOT EXISTS FOR (n:Organization) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT org_invitation_uid IF NOT EXISTS FOR (n:OrgInvitation) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT git_connection_uid IF NOT EXISTS FOR (n:GitConnection) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT git_connection_external_id IF NOT EXISTS FOR (n:GitConnection) REQUIRE n.external_id IS UNIQUE",
    "CREATE CONSTRAINT repo_uid IF NOT EXISTS FOR (n:Repository) REQUIRE n.uid IS UNIQUE",
    # Tenancy: slug is unique per org (application-enforced — composite
    # uniqueness needs Enterprise); the old global slug constraint is dropped
    # below. Likewise github_repo_id is unique per org only (also
    # application-enforced, in register-repo) — multiple orgs may each
    # register the same GitHub repo, so the old global constraint is dropped
    # and replaced by a plain lookup index (webhook fan-out queries it).
    "DROP CONSTRAINT repo_slug IF EXISTS",
    "DROP CONSTRAINT repo_github_id IF EXISTS",
    "CREATE CONSTRAINT event_uid IF NOT EXISTS FOR (n:Event) REQUIRE n.uid IS UNIQUE",
    # Workspace (sandbox) machinery
    "CREATE CONSTRAINT llm_provider_uid IF NOT EXISTS FOR (n:LLMProvider) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT sandbox_uid IF NOT EXISTS FOR (n:Sandbox) REQUIRE n.uid IS UNIQUE",
    # New domain model (PLATFORM.md primitives + RunPolicy)
    "CREATE CONSTRAINT agent_uid IF NOT EXISTS FOR (n:Agent) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT agent_revision_uid IF NOT EXISTS FOR (n:AgentRevision) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT scheduled_agent_uid IF NOT EXISTS FOR (n:ScheduledAgent) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT run_uid IF NOT EXISTS FOR (n:Run) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT finding_uid IF NOT EXISTS FOR (n:Finding) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT finding_dedupe IF NOT EXISTS FOR (n:Finding) REQUIRE n.dedupe_key IS UNIQUE",
    "CREATE CONSTRAINT run_policy_uid IF NOT EXISTS FOR (n:RunPolicy) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT platform_config_uid IF NOT EXISTS FOR (n:PlatformConfig) REQUIRE n.uid IS UNIQUE",
    # Delivery — PR convergence ledger (PLATFORM_V2_DESIGN.md)
    "CREATE CONSTRAINT pull_request_uid IF NOT EXISTS FOR (n:PullRequest) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT pull_request_key IF NOT EXISTS FOR (n:PullRequest) REQUIRE n.pr_key IS UNIQUE",
    "CREATE CONSTRAINT verdict_uid IF NOT EXISTS FOR (n:Verdict) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT resolution_uid IF NOT EXISTS FOR (n:FindingResolution) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT resolution_key IF NOT EXISTS FOR (n:FindingResolution) REQUIRE n.resolution_key IS UNIQUE",
    "CREATE CONSTRAINT merge_policy_uid IF NOT EXISTS FOR (n:MergePolicy) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT merge_policy_repo IF NOT EXISTS FOR (n:MergePolicy) REQUIRE n.repository_uid IS UNIQUE",
    "CREATE CONSTRAINT webhook_delivery_id IF NOT EXISTS FOR (n:WebhookDelivery) REQUIRE n.delivery_id IS UNIQUE",
    "CREATE CONSTRAINT ticket_uid IF NOT EXISTS FOR (n:Ticket) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT ticket_group_proposal_uid IF NOT EXISTS FOR (n:TicketGroupProposal) REQUIRE n.uid IS UNIQUE",
    # Analysis — the deep-scan report (one per Run, joined to its Findings)
    "CREATE CONSTRAINT analysis_uid IF NOT EXISTS FOR (n:Analysis) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT analysis_source_run IF NOT EXISTS FOR (n:Analysis) REQUIRE n.source_run_uid IS UNIQUE",
    # Comments — human notes on findings/tickets/PRs
    "CREATE CONSTRAINT comment_uid IF NOT EXISTS FOR (n:Comment) REQUIRE n.uid IS UNIQUE",
    # News — scout-filed items + user-entered interests
    "CREATE CONSTRAINT news_item_uid IF NOT EXISTS FOR (n:NewsItem) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT news_item_dedupe IF NOT EXISTS FOR (n:NewsItem) REQUIRE n.dedupe_key IS UNIQUE",
    "CREATE CONSTRAINT interest_uid IF NOT EXISTS FOR (n:Interest) REQUIRE n.uid IS UNIQUE",
    # Slack — per-org workspace connection + notification rules
    "CREATE CONSTRAINT slack_connection_uid IF NOT EXISTS FOR (n:SlackConnection) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT slack_connection_team IF NOT EXISTS FOR (n:SlackConnection) REQUIRE n.team_id IS UNIQUE",
    "CREATE CONSTRAINT slack_rule_uid IF NOT EXISTS FOR (n:SlackNotificationRule) REQUIRE n.uid IS UNIQUE",
    # Notifications — per-user read state for the in-app inbox. Uniqueness on
    # (user_uid, event_uid) via the composite `key` property (composite unique
    # constraints need Enterprise — same pattern as per-org repo slugs).
    "CREATE CONSTRAINT notification_read_key IF NOT EXISTS FOR (n:NotificationRead) REQUIRE n.key IS UNIQUE",
    # Schema migration ledger (infrastructure/migration_runner.py)
    "CREATE CONSTRAINT schema_migration_version IF NOT EXISTS FOR (n:SchemaMigration) REQUIRE n.version IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX repo_mode IF NOT EXISTS FOR (n:Repository) ON (n.mode)",
    "CREATE INDEX repo_github_id_idx IF NOT EXISTS FOR (n:Repository) ON (n.github_repo_id)",
    "CREATE INDEX repo_org IF NOT EXISTS FOR (n:Repository) ON (n.org_uid)",
    "CREATE INDEX user_org IF NOT EXISTS FOR (n:User) ON (n.org_uid)",
    "CREATE INDEX org_invitation_email IF NOT EXISTS FOR (n:OrgInvitation) ON (n.email)",
    "CREATE INDEX org_invitation_org IF NOT EXISTS FOR (n:OrgInvitation) ON (n.org_uid)",
    "CREATE INDEX git_connection_org IF NOT EXISTS FOR (n:GitConnection) ON (n.org_uid)",
    "CREATE INDEX git_connection_provider IF NOT EXISTS FOR (n:GitConnection) ON (n.provider)",
    "CREATE INDEX slack_connection_org IF NOT EXISTS FOR (n:SlackConnection) ON (n.org_uid)",
    "CREATE INDEX slack_rule_org IF NOT EXISTS FOR (n:SlackNotificationRule) ON (n.org_uid)",
    "CREATE INDEX slack_rule_event_type IF NOT EXISTS FOR (n:SlackNotificationRule) ON (n.event_type)",
    "CREATE INDEX repo_provider IF NOT EXISTS FOR (n:Repository) ON (n.provider)",
    "CREATE INDEX repo_slug_idx IF NOT EXISTS FOR (n:Repository) ON (n.slug)",
    "CREATE INDEX sandbox_status IF NOT EXISTS FOR (n:Sandbox) ON (n.status)",
    "CREATE INDEX sandbox_purpose IF NOT EXISTS FOR (n:Sandbox) ON (n.purpose)",
    "CREATE INDEX event_occurred_at IF NOT EXISTS FOR (n:Event) ON (n.occurred_at)",
    "CREATE INDEX event_subject IF NOT EXISTS FOR (n:Event) ON (n.subject_uid)",
    "CREATE INDEX event_kind IF NOT EXISTS FOR (n:Event) ON (n.kind)",
    "CREATE INDEX notification_read_user IF NOT EXISTS FOR (n:NotificationRead) ON (n.user_uid)",
    "CREATE INDEX notification_read_event IF NOT EXISTS FOR (n:NotificationRead) ON (n.event_uid)",
    "CREATE INDEX llm_provider_active IF NOT EXISTS FOR (n:LLMProvider) ON (n.active)",
    "CREATE INDEX llm_provider_org IF NOT EXISTS FOR (n:LLMProvider) ON (n.org_uid)",
    # Agents
    "CREATE INDEX agent_org IF NOT EXISTS FOR (n:Agent) ON (n.org_uid)",
    "CREATE INDEX agent_provenance IF NOT EXISTS FOR (n:Agent) ON (n.provenance)",
    "CREATE INDEX agent_revision_agent IF NOT EXISTS FOR (n:AgentRevision) ON (n.agent_uid)",
    "CREATE INDEX agent_revision_org IF NOT EXISTS FOR (n:AgentRevision) ON (n.org_uid)",
    "CREATE INDEX scheduled_agent_repo IF NOT EXISTS FOR (n:ScheduledAgent) ON (n.repository_uid)",
    "CREATE INDEX scheduled_agent_agent IF NOT EXISTS FOR (n:ScheduledAgent) ON (n.agent_uid)",
    "CREATE INDEX run_scheduled_agent IF NOT EXISTS FOR (n:Run) ON (n.scheduled_agent_uid)",
    "CREATE INDEX run_agent IF NOT EXISTS FOR (n:Run) ON (n.agent_uid)",
    "CREATE INDEX run_repo IF NOT EXISTS FOR (n:Run) ON (n.repository_uid)",
    "CREATE INDEX run_status IF NOT EXISTS FOR (n:Run) ON (n.status)",
    "CREATE INDEX run_executor IF NOT EXISTS FOR (n:Run) ON (n.executor)",
    "CREATE INDEX run_trigger IF NOT EXISTS FOR (n:Run) ON (n.trigger)",
    "CREATE INDEX run_playbook IF NOT EXISTS FOR (n:Run) ON (n.playbook)",
    "CREATE INDEX run_surface IF NOT EXISTS FOR (n:Run) ON (n.surface)",
    "CREATE INDEX run_linked_pr IF NOT EXISTS FOR (n:Run) ON (n.linked_pr_uid)",
    "CREATE INDEX run_linked_ticket IF NOT EXISTS FOR (n:Run) ON (n.linked_ticket_uid)",
    "CREATE INDEX run_linked_finding IF NOT EXISTS FOR (n:Run) ON (n.linked_finding_uid)",
    "CREATE INDEX run_sandbox IF NOT EXISTS FOR (n:Run) ON (n.sandbox_uid)",
    # Findings
    "CREATE INDEX finding_repo IF NOT EXISTS FOR (n:Finding) ON (n.repository_uid)",
    "CREATE INDEX finding_status IF NOT EXISTS FOR (n:Finding) ON (n.status)",
    "CREATE INDEX finding_kind IF NOT EXISTS FOR (n:Finding) ON (n.kind)",
    "CREATE INDEX finding_severity IF NOT EXISTS FOR (n:Finding) ON (n.severity)",
    "CREATE INDEX finding_subtype IF NOT EXISTS FOR (n:Finding) ON (n.subtype)",
    "CREATE INDEX finding_executor IF NOT EXISTS FOR (n:Finding) ON (n.executor)",
    "CREATE INDEX finding_source_run IF NOT EXISTS FOR (n:Finding) ON (n.source_run_uid)",
    "CREATE INDEX finding_detected_by_tool IF NOT EXISTS FOR (n:Finding) ON (n.detected_by_tool)",
    # Delivery
    "CREATE INDEX pull_request_repo IF NOT EXISTS FOR (n:PullRequest) ON (n.repository_uid)",
    "CREATE INDEX pull_request_state IF NOT EXISTS FOR (n:PullRequest) ON (n.state)",
    "CREATE INDEX pull_request_head IF NOT EXISTS FOR (n:PullRequest) ON (n.head_sha)",
    "CREATE INDEX verdict_pr IF NOT EXISTS FOR (n:Verdict) ON (n.pull_request_uid)",
    "CREATE INDEX resolution_pr IF NOT EXISTS FOR (n:FindingResolution) ON (n.pull_request_uid)",
    "CREATE INDEX resolution_finding IF NOT EXISTS FOR (n:FindingResolution) ON (n.finding_uid)",
    "CREATE INDEX resolution_state IF NOT EXISTS FOR (n:FindingResolution) ON (n.state)",
    "CREATE INDEX ticket_repo IF NOT EXISTS FOR (n:Ticket) ON (n.repository_uid)",
    "CREATE INDEX ticket_status IF NOT EXISTS FOR (n:Ticket) ON (n.status)",
    "CREATE INDEX ticket_group_proposal_repo IF NOT EXISTS FOR (n:TicketGroupProposal) ON (n.repository_uid)",
    "CREATE INDEX ticket_group_proposal_status IF NOT EXISTS FOR (n:TicketGroupProposal) ON (n.status)",
    # Analysis
    "CREATE INDEX analysis_repo IF NOT EXISTS FOR (n:Analysis) ON (n.repository_uid)",
    "CREATE INDEX analysis_status IF NOT EXISTS FOR (n:Analysis) ON (n.status)",
    # Comments
    "CREATE INDEX comment_subject IF NOT EXISTS FOR (n:Comment) ON (n.subject_uid)",
    # News
    "CREATE INDEX news_repo IF NOT EXISTS FOR (n:NewsItem) ON (n.repository_uid)",
    "CREATE INDEX news_status IF NOT EXISTS FOR (n:NewsItem) ON (n.status)",
    "CREATE INDEX news_category IF NOT EXISTS FOR (n:NewsItem) ON (n.category)",
    "CREATE INDEX news_source_run IF NOT EXISTS FOR (n:NewsItem) ON (n.source_run_uid)",
    "CREATE INDEX interest_repo IF NOT EXISTS FOR (n:Interest) ON (n.repository_uid)",
]


async def create_constraints() -> None:
    """Idempotent — safe to run on every startup."""
    for stmt in _CONSTRAINTS + _INDEXES:
        try:
            await adb.cypher_query(stmt)
        except Exception as exc:
            logger.warning(f"Constraint/index skipped ({exc}): {stmt}")
    logger.info("Constraints created", extra={"tag": "bootstrap"})
