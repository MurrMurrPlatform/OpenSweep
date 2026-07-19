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
