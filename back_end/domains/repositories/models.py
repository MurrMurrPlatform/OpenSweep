"""Repository + PlatformConfig nodes."""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


class Repository(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    # Tenancy root (domains/tenancy.py): the owning Organization. Every other
    # domain node reaches its org through repository_uid → this property.
    org_uid = StringProperty(required=True, index=True)
    # Unique within an org, not globally (application-enforced — Neo4j
    # Community has no composite constraints).
    slug = StringProperty(required=True, index=True)
    mode = StringProperty(required=True, choices={"github": "github"})
    # Git hosting provider key (infrastructure/git_providers). GitHub is the
    # only implementation today; pre-provider nodes are backfilled to
    # "github" by migration m0004.
    provider = StringProperty(default="github", index=True)
    name = StringProperty(required=True)
    description = StringProperty(default="")
    default_branch = StringProperty(default="main")
    color_scheme = StringProperty(default="indigo")
    is_active = BooleanProperty(default=True)

    # GitHub coordinates (owner/repo required at the API layer on create)
    github_owner = StringProperty()
    github_repo = StringProperty()
    github_repo_id = IntegerProperty()
    # GitHub App installation covering this repo (§7). Set/cleared by the
    # `installation` / `installation_repositories` webhooks; nullable — repos
    # without one fall back to the PAT.
    github_installation_id = IntegerProperty()
    # GitConnection(kind="pat") the repo was registered through — its token
    # authenticates this repo when no installation covers it. Nullable.
    git_connection_uid = StringProperty()
    github_connection_status = StringProperty()  # connected | disconnected | error
    last_synced_at = DateTimeProperty()

    # Freshness cursor: the last default-branch commit whose changed paths were
    # fed through mark_docs_stale / mark_areas_stale. Webhooks are the realtime
    # path; the reconcile tick compares this against the live head and replays
    # the gap, so a dropped delivery self-heals instead of silently leaving
    # every area "fresh" forever. Empty on a newly registered repo — it adopts
    # the current head WITHOUT marking anything stale (a new repo is not
    # retroactively stale for all of its history).
    freshness_synced_sha = StringProperty(default="")
    freshness_synced_at = DateTimeProperty()
    # Non-empty when the last freshness pass could not see everything (compare
    # truncated at 300 files, provider unavailable, per-node save errors). The
    # UI must show this instead of a confident "all fresh".
    freshness_degraded_reason = StringProperty(default="")

    metadata = JSONProperty(default={})

    # Per-repo workflow config: pipeline stage → {agent_uid, auto}.
    # Stages mirror the run playbooks (ask/discover/review/fix/implement/
    # verify/document). See domains/repositories/services/workflow.py.
    workflow = JSONProperty(default={})

    # Static-analysis candidates config: {mode: auto|custom|off, tools:
    # [{tool, args, paths}]}. See domains/repositories/services/analyzer_config.py
    # and domains/execution/services/static_analysis.py.
    analyzers = JSONProperty(default={})

    # PLATFORM.md §Run policies: per-repo halt switch for all autonomous and
    # pending Run dispatches. Human-triggered runs still see a 409.
    kill_switch_active = BooleanProperty(default=False)

    # Agent autonomy: when true, agents (via the platform-tool MCP surface) may
    # perform operations that are otherwise human-only for this repo — notably
    # moving tickets through the status matrix, including the Gate-1 approval
    # (backlog → todo). Off by default: an agent must never approve its own
    # proposed work unless the operator has opted this repo into autonomy.
    agent_autonomy = BooleanProperty(default=False)

    # DISTINCT from `agent_autonomy` above, despite the shared word. That one
    # is a permission ("agents may move tickets"); this is the question-policy
    # tier for thread/triage runs: interrogate|assume|strict. "" = inherit the
    # platform default (interrogate). See domains/runs/services/autonomy.py.
    default_autonomy = StringProperty(default="")

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


class PlatformConfig(AsyncStructuredNode):
    """Singleton (uid='singleton') carrying global runtime knobs.

    Currently: the global kill switch. Avoid sprawling into a god-object —
    new flags should justify themselves first.
    """

    uid = StringProperty(unique_index=True, required=True)
    global_kill_switch = BooleanProperty(default=False)
    updated_at = DateTimeProperty(default_now=True)
