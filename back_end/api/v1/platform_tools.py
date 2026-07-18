"""HTTP transport for the tracking-safe platform tools.

Non-Python / external executors invoke platform tools through these
endpoints. Authorization: in v1 we trust the local user (same as the rest
of the API); future auth integrates here.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_current_user
from api.platform_scope import require_tool_repo_access, require_tool_run_access
from domains.findings.models import Finding
from domains.findings.schemas import (
    Effort,
    FindingKind,
    ParseStatus,
    Severity,
    SourcePath,
)
from domains.platform_tools.add_analysis_note import add_analysis_note
from domains.platform_tools.ask_question import ask_question
from domains.platform_tools.attach_artifact import attach_artifact
from domains.platform_tools.complete_run import complete_run
from domains.platform_tools.create_finding import create_finding
from domains.platform_tools.docs_tools import confirm_doc_current, propose_doc_edit
from domains.platform_tools.memory_tools import write_memory
from domains.platform_tools.news_tools import create_news_item
from domains.platform_tools.set_analysis_section import set_analysis_section
from domains.platform_tools.submit_thread_plan import submit_thread_plan
from domains.platform_tools.update_finding import update_finding
from domains.platform_tools.upsert_analysis import upsert_analysis
from domains.platform_tools.web_tools import fetch_url, web_search
from domains.users.schemas import UserDTO
from logging_config import logger

router = APIRouter(prefix="/api/v1/platform-tools", tags=["platform_tools"])


class CreateFindingRequest(BaseModel):
    repository_uid: str
    tags: list[str] = Field(default_factory=list)
    kind: FindingKind = FindingKind.DEFECT
    severity: Severity = Severity.MEDIUM
    effort: Effort = Effort.MEDIUM
    subtype: str = ""
    title: str
    confidence: float = 0.7
    description: str = Field(
        "",
        description=(
            "Detailed analysis of the problem: what is wrong, where, and how it "
            "manifests. Rendered as markdown — use code spans, fenced blocks, and "
            "lists freely."
        ),
    )
    root_cause: str = Field(
        "",
        description=(
            "Why the problem exists — the underlying mechanism, not the symptom. "
            "Rendered as markdown."
        ),
    )
    why_it_matters: str = Field(
        "",
        description=(
            "Impact if left unfixed (user-facing, security, operational). "
            "Rendered as markdown."
        ),
    )
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str = Field(
        "",
        description=(
            "Concrete remediation steps. Rendered as markdown — use fenced code "
            "blocks for code suggestions."
        ),
    )
    affected_paths: list[str] = Field(default_factory=list)
    detected_by_tool: str = Field(
        "",
        description=(
            "Static-analysis provenance. When you file this finding after "
            "investigating a 'Static-analysis candidates' line, set this to that "
            "candidate's tool (ruff, vulture, deptry, semgrep, knip). Leave empty "
            "for a finding you discovered on your own."
        ),
    )
    detected_by_rule: str = Field(
        "",
        description=(
            "The candidate's rule/check id (e.g. 'F821', a semgrep check_id), "
            "copied verbatim so the finding cross-references the raw tool output."
        ),
    )
    source_run_uid: Optional[str] = None
    executor: str = "manual"
    source_path: SourcePath = SourcePath.TOOL_CALL
    parse_status: ParseStatus = ParseStatus.OK


class UpdateFindingRequest(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    actor: str = "manual"


class ProposeDocEditRequest(BaseModel):
    repository_uid: str
    proposed_body: str
    rationale: str = ""
    slug: str = ""
    title: str = ""
    summary: str = ""
    watch_paths: list[str] = Field(default_factory=list)
    source_run_uid: Optional[str] = None
    executor: str = "manual"


class ConfirmDocCurrentRequest(BaseModel):
    repository_uid: str
    slug: str


class WriteMemoryRequest(BaseModel):
    repository_uid: str
    title: str
    body: str = ""
    anchor_uid: str = ""
    source_run_uid: Optional[str] = None
    executor: str = "manual"


class AttachArtifactRequest(BaseModel):
    target_uid: str
    target_type: str
    artifact_type: str
    content: str
    repository_uid: Optional[str] = None
    extension: str = "txt"
    summary: str = ""
    executor: str = "manual"


class CompleteRunRequest(BaseModel):
    summary: str = Field("", description="Short prose summary of the run outcome.")
    # Structured end-of-run breakdown — stored on the Run node and shown in
    # the UI. Each entry is one short sentence.
    did: list[str] = Field(default_factory=list, description="What was done.")
    skipped: list[str] = Field(default_factory=list, description="What was skipped and why.")
    succeeded: list[str] = Field(default_factory=list, description="What succeeded.")
    failed: list[str] = Field(default_factory=list, description="What failed and why.")
    next_steps: list[str] = Field(
        default_factory=list, description="Next steps or future suggestions."
    )
    output_refs: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    raw_artifact_uri: Optional[str] = None
    parse_status: Optional[str] = None
    error: Optional[str] = None
    final_status: str = "awaiting_input"


class UpsertAnalysisRequest(BaseModel):
    repository_uid: str
    title: Optional[str] = Field(None, description="Report title, e.g. 'Deep scan — whole repository'.")
    status: Optional[str] = Field(
        None, description="in_progress | complete | superseded | archived."
    )
    revision: Optional[str] = Field(None, description="Commit sha the scan inspected.")
    health_grade: Optional[str] = Field(None, description="Overall grade A | B | C | D | F.")
    health_score: Optional[int] = Field(None, description="Optional overall score 0-100.")
    scorecard: Optional[list[dict[str, Any]]] = Field(
        None,
        description=(
            "Per-dimension rubric: [{dimension, score, max, grade, rationale}] "
            "across correctness, security, performance, testing, architecture, …"
        ),
    )
    confidence: Optional[str] = Field(
        None, description="Overall confidence: confirmed | high | medium | low."
    )
    limitations: Optional[str] = Field(None, description="Markdown: what limited the analysis.")
    stats: Optional[dict[str, Any]] = Field(
        None, description="Free-form counts (findings_by_severity, files_scanned, …); merged."
    )
    executor: str = "manual"


class SetAnalysisSectionRequest(BaseModel):
    repository_uid: str
    section: str = Field(
        ...,
        description=(
            "Section key: executive_summary, repository_map, security_summary, "
            "performance_summary, dependency_report, test_gap_report, "
            "implementation_plan, top_changes, or any custom key."
        ),
    )
    content: str = Field("", description="The section body, as markdown.")
    executor: str = "manual"


class AddAnalysisNoteRequest(BaseModel):
    repository_uid: str
    note_type: str = Field(..., description="coverage | strength | validation.")
    # coverage
    area: str = ""
    paths: list[str] = Field(default_factory=list)
    status: str = Field("examined", description="Coverage status: examined | partial | skipped.")
    note: str = ""
    # strength
    title: str = ""
    detail: str = ""
    # validation
    check: str = ""
    command: str = ""
    result: str = ""
    details: str = ""
    executor: str = "manual"


class CreateNewsItemToolRequest(BaseModel):
    repository_uid: str
    title: str
    url: str = ""
    source: str = Field(
        "manual",
        description="Where the story was found: searxng | github | hackernews | arxiv | manual.",
    )
    category: str = Field(
        "industry",
        description=(
            "trending-repo | ai-news | framework | technique | research | "
            "tooling | industry."
        ),
    )
    summary: str = Field(
        "", description="Markdown: what the item itself is about."
    )
    relevance: str = Field(
        "",
        description=(
            "Markdown: why THIS repository's team should care — the field "
            "humans triage by. Ground it in the repo's stack/findings/interests."
        ),
    )
    tags: list[str] = Field(default_factory=list)
    published_at: Optional[str] = Field(
        None, description="ISO-8601 timestamp of the original story, if known."
    )
    source_run_uid: Optional[str] = None


class WebSearchRequest(BaseModel):
    repository_uid: str
    query: str
    mode: str = Field(
        "web", description="web (SearXNG) | github | hackernews | arxiv | trendshift."
    )
    limit: int = 8


class FetchUrlRequest(BaseModel):
    repository_uid: str
    url: str


class AskQuestionRequest(BaseModel):
    repository_uid: str
    question: str = Field(..., description="The unresolved question for a human to answer.")
    why_it_matters: str = Field("", description="Why the answer matters / what it unblocks.")
    category: str = ""
    executor: str = "manual"


async def _artifact_target_repository_uid(target_uid: str, target_type: str) -> str:
    """Resolve an attach-artifact target to its repository so the call can be
    tenancy-gated like every other entity-keyed tool. 404 on unknown targets."""
    from domains.delivery.models import PullRequest
    from domains.docs.models import Doc
    from domains.investigations.models import Run
    from domains.memory.models import Memory
    from domains.tickets.models import Ticket

    models = {
        "run": Run,
        "finding": Finding,
        "doc": Doc,
        "memory": Memory,
        "ticket": Ticket,
        "pull_request": PullRequest,
        "pullrequest": PullRequest,
    }
    model = models.get(target_type.strip().lower())
    node = await model.nodes.get_or_none(uid=target_uid) if model else None
    if node is None:
        raise HTTPException(status_code=404, detail="not found")
    return node.repository_uid


async def _invoke_platform_tool(tool_name: str, func, **kwargs):
    try:
        return await func(**kwargs)
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            f"Platform tool failed: {tool_name}",
            extra={"tag": "platform-tools"},
        )
        raise


@router.post("/create-finding", operation_id="opensweep_platform_create_finding")
async def http_create_finding(
    req: CreateFindingRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = req.model_dump(mode="json")
    data["source_run_uid"] = data.get("source_run_uid") or request.headers.get(
        "x-opensweep-run-uid"
    )
    return await _invoke_platform_tool("create_finding", create_finding, **data)


@router.post("/update-finding/{finding_uid}", operation_id="opensweep_platform_update_finding")
async def http_update_finding(
    finding_uid: str,
    req: UpdateFindingRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    finding = await Finding.nodes.get_or_none(uid=finding_uid)
    if finding is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, finding.repository_uid)
    return await _invoke_platform_tool(
        "update_finding",
        update_finding,
        finding_uid=finding_uid,
        changes=req.changes,
        actor=req.actor,
    )


class AskUserBody(BaseModel):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=6)
    context: str = ""


@router.post(
    "/ask-user/{thread_uid}",
    operation_id="opensweep_platform_ask_user",
)
async def http_ask_user(
    thread_uid: str,
    req: AskUserBody,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    from domains.platform_tools.ask_user import ask_user
    from domains.threads.services.thread_service import (
        THREAD_NOT_FOUND_DETAIL,
        resolve_thread,
    )

    executor = request.headers.get("x-opensweep-run-uid") or "manual"
    thread = await resolve_thread(thread_uid, run_uid=executor)
    if thread is None:
        raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
    await require_tool_repo_access(request, user, thread.repository_uid)
    return await _invoke_platform_tool(
        "ask_user",
        ask_user,
        thread_uid=thread.uid,
        question=req.question,
        options=req.options,
        context=req.context,
        executor=executor,
    )


class SubmitThreadPlanBody(BaseModel):
    plan_markdown: str = Field(min_length=1)


@router.post(
    "/submit-thread-plan/{thread_uid}",
    operation_id="opensweep_platform_submit_thread_plan",
)
async def http_submit_thread_plan(
    thread_uid: str,
    req: SubmitThreadPlanBody,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    from domains.threads.services.thread_service import (
        THREAD_NOT_FOUND_DETAIL,
        resolve_thread,
    )

    executor = request.headers.get("x-opensweep-run-uid") or "manual"
    thread = await resolve_thread(thread_uid, run_uid=executor)
    if thread is None:
        raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
    await require_tool_repo_access(request, user, thread.repository_uid)
    return await _invoke_platform_tool(
        "submit_thread_plan",
        submit_thread_plan,
        thread_uid=thread.uid,
        plan_markdown=req.plan_markdown,
        executor=executor,
    )


@router.post("/propose-doc-edit", operation_id="opensweep_platform_propose_doc_edit")
async def http_propose_doc_edit(
    req: ProposeDocEditRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = req.model_dump()
    data["source_run_uid"] = data.get("source_run_uid") or request.headers.get(
        "x-opensweep-run-uid"
    )
    return await _invoke_platform_tool("propose_doc_edit", propose_doc_edit, **data)


@router.post("/write-memory", operation_id="opensweep_platform_write_memory")
async def http_write_memory(
    req: WriteMemoryRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = req.model_dump()
    data["source_run_uid"] = data.get("source_run_uid") or request.headers.get(
        "x-opensweep-run-uid"
    )
    return await _invoke_platform_tool("write_memory", write_memory, **data)


@router.post(
    "/confirm-doc-current", operation_id="opensweep_platform_confirm_doc_current"
)
async def http_confirm_doc_current(
    req: ConfirmDocCurrentRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    return await _invoke_platform_tool(
        "confirm_doc_current", confirm_doc_current, **req.model_dump()
    )


@router.post("/attach-artifact", operation_id="opensweep_platform_attach_artifact")
async def http_attach_artifact(
    req: AttachArtifactRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    repository_uid = req.repository_uid or await _artifact_target_repository_uid(
        req.target_uid, req.target_type
    )
    await require_tool_repo_access(request, user, repository_uid)
    return await _invoke_platform_tool(
        "attach_artifact", attach_artifact, **req.model_dump()
    )


@router.post("/complete-run/{run_uid}", operation_id="opensweep_platform_complete_run")
async def http_complete_run(
    run_uid: str,
    req: CompleteRunRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_run_access(request, user, run_uid)
    return await _invoke_platform_tool(
        "complete_run", complete_run, run_uid=run_uid, **req.model_dump()
    )


def _with_run_header(data: dict, request: Request) -> dict:
    """Inject the run-scoped source_run_uid header — the Analysis key."""
    data["source_run_uid"] = request.headers.get("x-opensweep-run-uid") or ""
    return data


@router.post("/upsert-analysis", operation_id="opensweep_platform_upsert_analysis")
async def http_upsert_analysis(
    req: UpsertAnalysisRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = _with_run_header(req.model_dump(mode="json"), request)
    return await _invoke_platform_tool("upsert_analysis", upsert_analysis, **data)


@router.post("/set-analysis-section", operation_id="opensweep_platform_set_analysis_section")
async def http_set_analysis_section(
    req: SetAnalysisSectionRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = _with_run_header(req.model_dump(mode="json"), request)
    return await _invoke_platform_tool(
        "set_analysis_section", set_analysis_section, **data
    )


@router.post("/add-analysis-note", operation_id="opensweep_platform_add_analysis_note")
async def http_add_analysis_note(
    req: AddAnalysisNoteRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = _with_run_header(req.model_dump(mode="json"), request)
    return await _invoke_platform_tool("add_analysis_note", add_analysis_note, **data)


@router.post("/create-news-item", operation_id="opensweep_platform_create_news_item")
async def http_create_news_item(
    req: CreateNewsItemToolRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = req.model_dump(mode="json")
    data["source_run_uid"] = data.get("source_run_uid") or request.headers.get(
        "x-opensweep-run-uid"
    )
    return await _invoke_platform_tool("create_news_item", create_news_item, **data)


@router.post("/web-search", operation_id="opensweep_platform_web_search")
async def http_web_search(
    req: WebSearchRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    return await _invoke_platform_tool(
        "web_search", web_search, query=req.query, mode=req.mode, limit=req.limit
    )


@router.post("/fetch-url", operation_id="opensweep_platform_fetch_url")
async def http_fetch_url(
    req: FetchUrlRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    return await _invoke_platform_tool("fetch_url", fetch_url, url=req.url)


@router.post("/ask-question", operation_id="opensweep_platform_ask_question")
async def http_ask_question(
    req: AskQuestionRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    await require_tool_repo_access(request, user, req.repository_uid)
    data = _with_run_header(req.model_dump(mode="json"), request)
    return await _invoke_platform_tool("ask_question", ask_question, **data)
