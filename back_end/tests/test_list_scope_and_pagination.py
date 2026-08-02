"""List endpoints filter by tenant in the query, and can be paged.

WHY: every cross-repo list used to load a whole node label and drop the
other tenants' rows in Python afterwards —

    items = await XService().list()                    # every X in the graph
    items = [x for x in items if x.repository_uid in allowed]

— so one org's board read every org's data, and the cost of a page load grew
with the whole instance rather than with the caller's org. Nothing was
paginated either, so the response grew without limit too.

WHAT this pins:
  - `repo_scope` resolves a route's repository_uid + org into an allow-list;
  - the service `list()` methods take that allow-list, put it in the query as
    `repository_uid__in`, and return nothing when it is empty (an empty scope
    must never read as "no filter");
  - omitting the scope is a TypeError, not a silent cross-tenant scan;
  - `paginate` windows the result and reports the full size in a header.

DB-free: the services are called with a fake `nodes` that records the query.
"""

from types import SimpleNamespace

import pytest
from fastapi import Response

from domains.pagination import MAX_LIMIT, Page, page_params, paginate

pytestmark = pytest.mark.asyncio


class _RecordingNodes:
    """Stands in for `Model.nodes`, capturing the filter kwargs."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[dict] = []

    async def filter(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)

    async def all(self):
        raise AssertionError("a list endpoint loaded a whole label")


# ── repo_scope ───────────────────────────────────────────────────────────────


async def test_repo_scope_narrows_to_the_named_repo(monkeypatch):
    import domains.tenancy as tenancy

    async def boom(org_uid):
        raise AssertionError("named a repo, so the org's repos are not needed")

    monkeypatch.setattr(tenancy, "org_repo_uids", boom)
    assert await tenancy.repo_scope("repo-a", "org-a") == ["repo-a"]


async def test_repo_scope_falls_back_to_the_whole_org(monkeypatch):
    import domains.tenancy as tenancy

    async def org_repos(org_uid):
        return {"repo-b", "repo-a"}

    monkeypatch.setattr(tenancy, "org_repo_uids", org_repos)
    assert await tenancy.repo_scope(None, "org-a") == ["repo-a", "repo-b"]


async def test_repo_scope_of_an_org_with_no_repos_is_empty(monkeypatch):
    import domains.tenancy as tenancy

    async def org_repos(org_uid):
        return set()

    monkeypatch.setattr(tenancy, "org_repo_uids", org_repos)
    assert await tenancy.repo_scope(None, "org-a") == []


# ── the services put the scope in the query ──────────────────────────────────

# (module, attribute holding the model, callable returning the coroutine)
_SERVICES = [
    (
        "domains.findings.services.finding_service",
        "Finding",
        lambda mod, scope: mod.FindingService().list(repository_uids=scope),
    ),
    (
        "domains.tickets.services.ticket_service",
        "Ticket",
        lambda mod, scope: mod.TicketService().list(repository_uids=scope),
    ),
    (
        "domains.tickets.services.epic_service",
        "EpicProposal",
        lambda mod, scope: mod.EpicService().list(repository_uids=scope),
    ),
    (
        "domains.delivery.services.pull_request_service",
        "PullRequest",
        lambda mod, scope: mod.PullRequestService().list(repository_uids=scope),
    ),
    (
        "domains.news.services.news_service",
        "NewsItem",
        lambda mod, scope: mod.NewsService().list(repository_uids=scope),
    ),
    (
        "domains.news.services.interest_service",
        "Interest",
        lambda mod, scope: mod.InterestService().list(repository_uids=scope),
    ),
    (
        "domains.analysis.services.analysis_service",
        "Analysis",
        lambda mod, scope: mod.AnalysisService().list(repository_uids=scope),
    ),
    (
        "domains.agents.services.scheduled_agent_service",
        "ScheduledAgent",
        lambda mod, scope: mod.list_scheduled_agents(repository_uids=scope),
    ),
    (
        "domains.execution.services.sandbox_service",
        "Sandbox",
        lambda mod, scope: mod.SandboxService().list_active(repository_uids=scope),
    ),
]


def _patch_model(monkeypatch, module_path: str, model_attr: str) -> _RecordingNodes:
    import importlib

    mod = importlib.import_module(module_path)
    nodes = _RecordingNodes()
    monkeypatch.setattr(mod, model_attr, SimpleNamespace(nodes=nodes))
    return mod, nodes


@pytest.mark.parametrize("module_path,model_attr,call", _SERVICES)
async def test_scope_goes_into_the_query(monkeypatch, module_path, model_attr, call):
    mod, nodes = _patch_model(monkeypatch, module_path, model_attr)
    await call(mod, ["repo-a", "repo-b"])
    assert nodes.calls, f"{module_path} ran no query"
    assert nodes.calls[0].get("repository_uid__in") == ["repo-a", "repo-b"], (
        f"{module_path} did not push the tenancy filter into the query"
    )


@pytest.mark.parametrize("module_path,model_attr,call", _SERVICES)
async def test_an_empty_scope_reads_nothing(monkeypatch, module_path, model_attr, call):
    # The dangerous failure mode: an empty allow-list treated as "no filter",
    # which returns every tenant's rows instead of none.
    mod, nodes = _patch_model(monkeypatch, module_path, model_attr)
    assert await call(mod, []) == []
    assert nodes.calls == [], f"{module_path} queried despite an empty scope"


# The bound `list` callable for each service, for signature inspection.
_LIST_FUNCS = [
    ("FindingService.list", "domains.findings.services.finding_service", "FindingService.list"),
    ("TicketService.list", "domains.tickets.services.ticket_service", "TicketService.list"),
    ("EpicService.list", "domains.tickets.services.epic_service", "EpicService.list"),
    (
        "PullRequestService.list",
        "domains.delivery.services.pull_request_service",
        "PullRequestService.list",
    ),
    ("NewsService.list", "domains.news.services.news_service", "NewsService.list"),
    ("InterestService.list", "domains.news.services.interest_service", "InterestService.list"),
    ("AnalysisService.list", "domains.analysis.services.analysis_service", "AnalysisService.list"),
    (
        "list_scheduled_agents",
        "domains.agents.services.scheduled_agent_service",
        "list_scheduled_agents",
    ),
    (
        "SandboxService.list_active",
        "domains.execution.services.sandbox_service",
        "SandboxService.list_active",
    ),
]


@pytest.mark.parametrize("name,module_path,dotted", _LIST_FUNCS)
def test_the_scope_is_required(name, module_path, dotted):
    """No default for repository_uids — forgetting it must fail loudly.

    A default of None or [] would restore exactly the bug this change
    removes: a caller that omits the scope silently reading every tenant.
    """
    import importlib
    import inspect

    obj = importlib.import_module(module_path)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    param = inspect.signature(obj).parameters["repository_uids"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
    assert param.default is inspect.Parameter.empty, f"{name} has a default scope"


# ── routes must vet the repo they scope to ───────────────────────────────────


def test_every_route_scoping_to_a_named_repo_checks_the_org_first():
    """`repo_scope(repository_uid, org)` returns [repository_uid] as-is.

    It does NOT check that the repo belongs to the org — that is
    require_repo_in_org's job, and it must already have run. A route that
    passes a caller-supplied repository_uid to repo_scope without it reads
    another tenant's rows on request.
    """
    import ast
    import pathlib

    api_v1 = pathlib.Path(__file__).resolve().parents[1] / "api" / "v1"
    offenders = []
    for path in sorted(api_v1.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {
                node.func.id
                for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            if "repo_scope" not in called:
                continue
            # A literal first argument (e.g. repo_scope(None, org)) is the
            # org-wide form and needs no per-repo check.
            scopes_a_variable = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "repo_scope"
                and node.args
                and not isinstance(node.args[0], ast.Constant)
                for node in ast.walk(fn)
            )
            if scopes_a_variable and "require_repo_in_org" not in called:
                offenders.append(f"{path.name}:{fn.lineno} {fn.name}")
    assert offenders == [], f"repo_scope on an unvetted repository_uid: {offenders}"


# ── paginate ─────────────────────────────────────────────────────────────────


def test_no_limit_returns_everything():
    items = list(range(250))
    assert paginate(items, Page(), Response()) == items


def test_a_window_is_cut_out():
    assert paginate(list(range(10)), Page(limit=3, offset=4), Response()) == [4, 5, 6]


def test_an_offset_past_the_end_is_empty():
    assert paginate(list(range(3)), Page(limit=5, offset=99), Response()) == []


def test_offset_without_limit_still_skips():
    assert paginate(list(range(5)), Page(offset=3), Response()) == [3, 4]


def test_the_full_size_is_reported_not_the_page_size():
    response = Response()
    page = paginate(list(range(250)), Page(limit=10), response)
    assert len(page) == 10
    # Without this a caller cannot tell a last page from a full one.
    assert response.headers["x-total-count"] == "250"


def test_page_params_defaults_to_no_window():
    assert page_params(limit=None, offset=0) == Page(limit=None, offset=0)


def test_max_limit_is_bounded():
    assert 0 < MAX_LIMIT <= 1000


# The list routes this change paginates. Their `limit` reaches either a
# Cypher window (/audit) or `paginate`, so each needs a declared lower bound.
_PAGINATED_PATHS = [
    "/api/v1/findings",
    "/api/v1/tickets",
    "/api/v1/epic-proposals",
    "/api/v1/delivery/pull-requests",
    "/api/v1/news",
    "/api/v1/analyses",
    "/api/v1/runs",
    "/api/v1/audit",
]


def test_a_negative_limit_is_rejected_at_the_edge():
    """A negative limit must 422, never reach the database.

    While the limit was a Python slice, `limit=-1` merely dropped the last
    row. /api/v1/audit now puts the window in Cypher, and Neo4j rejects a
    negative LIMIT with a client error — a 500 for what should be a 422.

    `limit=0` stays legal on the two routes that always had a limit: it was
    an empty page before and Cypher `LIMIT 0` is valid, so the bound is
    ge=0 there and ge=1 only on the newly added, optional `page_params`.
    """
    import os

    os.environ.setdefault("ZITADEL_ISSUER", "http://localhost:8300")
    os.environ.setdefault("ZITADEL_CLIENT_ID", "test")
    os.environ.setdefault("OPENSWEEP_AUTH_TOKEN", "test")
    from app import app

    paths = app.openapi()["paths"]
    unbounded = []
    for path in _PAGINATED_PATHS:
        schema = next(
            p["schema"]
            for p in paths[path]["get"]["parameters"]
            if p["name"] == "limit"
        )
        # Optional params render as anyOf[integer, null]; required ones inline.
        variants = schema.get("anyOf", [schema])
        if not any("minimum" in v for v in variants):
            unbounded.append(path)
    assert unbounded == [], f"limit with no lower bound: {unbounded}"
