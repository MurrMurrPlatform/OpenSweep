"""News routes — the repo's news board (list, triage, convert) plus the two
run-triggering buttons: news scan and doc-proposal.

Conversion of a NewsItem into a Finding is human-only; the news-scout agent
never files findings or tickets — it only files NewsItems."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.agents.services.seed_variants import variant_prompt_body
from domains.findings.schemas import FindingDTO
from domains.runs.schemas import (
    Effort,
    RunDTO,
    RunTrigger,
)
from domains.runs.services.lifecycle import LifecycleError, trigger_run
from domains.runs.services.turn_service import run_to_dto
from domains.news.schemas import (
    ConvertNewsRequest,
    CreateNewsItemRequest,
    NewsItemDTO,
    UpdateNewsItemRequest,
)
from domains.news.services.interest_service import InterestService
from domains.news.services.news_service import NewsService
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/news", tags=["news"])


class TriggerNewsRunRequest(BaseModel):
    repository_uid: str = Field(min_length=1)


# ── Intent builders (module-level for unit-testability) ─────────────────────

_SCAN_FALLBACK_INTENT = (
    "You are the news scout for this repository. Survey what is new and "
    "relevant to this repo's stack and the interests listed below.\n"
    "\n"
    "Use `list_interests` and `list_news_items` to see what is watched and "
    "what is already filed, then research with `web_search` and `fetch_url`.\n"
    "File each genuinely new, relevant item via `create_news_item` (title, "
    "url, category, summary, and a repo-specific relevance note).\n"
    "NEVER call `create_finding` or `create_ticket` — converting news into "
    "findings is a human-only action taken from the news board."
)


async def _build_news_scan_intent(repository_uid: str) -> str:
    """News-scout intent: seeded variant body (or fallback) + rendered
    interests + the current board so the agent does not re-file items."""
    base = await variant_prompt_body("news-scout") or _SCAN_FALLBACK_INTENT

    interests = await InterestService().list(
        repository_uid=repository_uid, enabled_only=True
    )
    interest_lines = (
        "\n".join(f"- {i.title}: {i.details}" for i in interests)
        or "(none entered yet)"
    )

    existing = await NewsService().list(repository_uid=repository_uid)
    existing_lines = (
        "\n".join(f"- {n.title} ({n.url})" for n in existing[:20]) or "(none yet)"
    )

    return (
        f"{base}\n"
        "\n"
        "## Interests to watch\n"
        f"{interest_lines}\n"
        "\n"
        "## Already on the news board (do not re-file)\n"
        f"{existing_lines}"
    )


# Doc-proposal intent: research best practices / AI landscape / industry
# movement and deliver exactly one propose_doc_edit for the landscape doc.
# Constant — repository context arrives through the run's repo.
_DOC_PROPOSAL_INTENT = (
    "Produce ONE full replacement proposal for this repository's "
    "'Best practices & AI landscape' document.\n"
    "\n"
    "Step 1 — Ground yourself in the repository:\n"
    "- Read the current docs with `list_docs` / `read_doc` to understand "
    "the stack, conventions, and what is already documented.\n"
    "- Read the news board with `list_news_items` (statuses `new` and "
    "`saved`) and the watched topics with `list_interests`.\n"
    "\n"
    "Step 2 — Do fresh research with `web_search` and `fetch_url` on:\n"
    "- current best practices for this repository's stack,\n"
    "- the AI landscape (models, tooling, techniques) relevant to it,\n"
    "- industry movement relevant to this repo's domain and the listed "
    "interests.\n"
    "\n"
    "Step 3 — Deliver EXACTLY ONE `propose_doc_edit` call with:\n"
    "- slug: `insights/industry-landscape`\n"
    "- title: `Best practices & AI landscape`\n"
    "- a full replacement markdown body with these sections, each claim "
    "tied to a source URL:\n"
    "  1. Current best practices for this stack\n"
    "  2. AI landscape\n"
    "  3. Industry movement\n"
    "  4. Recommended actions\n"
    "\n"
    "The proposal lands as a pending doc edit for a human to accept or "
    "reject — do not expect it to apply immediately.\n"
    "Do NOT file findings (`create_finding`) or news items "
    "(`create_news_item`) in this run; this run's only deliverable is the "
    "single doc proposal. Finish with `complete_run`."
)


# ── Collection routes ────────────────────────────────────────────────────────


@router.get("", response_model=list[NewsItemDTO], operation_id="opensweep_list_news_items")
async def list_news_items(
    repository_uid: str | None = Query(None),
    category: str | None = Query(None),
    status: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    items = await NewsService().list(
        repository_uid=repository_uid,
        category=category,
        status=status,
    )
    if repository_uid is None:
        allowed = await org_repo_uids(user.org_uid)
        items = [n for n in items if n.repository_uid in allowed]
    return items


@router.post("", response_model=NewsItemDTO, operation_id="opensweep_create_news_item")
async def create_news_item(
    req: CreateNewsItemRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    dto, _deduplicated = await NewsService().create(req, actor_uid=user.uid)
    return dto


# NOTE: /scan and /doc-proposal are declared BEFORE the /{uid} routes so the
# literal paths win over the parameterised one.


@router.post("/scan", response_model=RunDTO, operation_id="opensweep_trigger_news_scan")
async def trigger_news_scan(
    req: TriggerNewsRunRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    # Specialized ask run: the news-scout template IS the instructions
    # (custom_intent) — org append guidance and framing still stack.
    from domains.agents.services.composition import compose_agent_intent

    composed = await compose_agent_intent(
        repository_uid=req.repository_uid,
        agent_key="ask",
        stage="ask",
        repo_guidance="",
        custom_intent=await _build_news_scan_intent(req.repository_uid),
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(Effort.NORMAL)

    await write_audit(
        kind="news.scan.requested",
        subject_type="Repository",
        subject_uid=req.repository_uid,
        actor_uid=user.uid,
    )

    try:
        run = await trigger_run(
            repository_uid=req.repository_uid,
            intent=intent,
            playbook="ask",
            title="News scan",
            target={"news_scan": True},
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return run_to_dto(run)


@router.post(
    "/doc-proposal",
    response_model=RunDTO,
    operation_id="opensweep_trigger_news_doc_proposal",
)
async def trigger_news_doc_proposal(
    req: TriggerNewsRunRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    # Specialized document run: the doc-proposal template IS the
    # instructions (custom_intent) — org append guidance still stacks.
    from domains.agents.services.composition import compose_agent_intent

    composed = await compose_agent_intent(
        repository_uid=req.repository_uid,
        agent_key="document",
        stage="document",
        repo_guidance="",
        custom_intent=_DOC_PROPOSAL_INTENT,
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(Effort.NORMAL)

    await write_audit(
        kind="news.doc_proposal.requested",
        subject_type="Repository",
        subject_uid=req.repository_uid,
        actor_uid=user.uid,
    )

    try:
        run = await trigger_run(
            repository_uid=req.repository_uid,
            intent=intent,
            playbook="document",
            title="Best practices & AI landscape doc proposal",
            target={"news_doc_proposal": True},
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return run_to_dto(run)


# ── Item routes ──────────────────────────────────────────────────────────────


@router.get("/{uid}", response_model=NewsItemDTO, operation_id="opensweep_get_news_item")
async def get_news_item(uid: str, user: UserDTO = Depends(get_current_user)):
    n = await NewsService().get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    return await NewsService().get(uid)


@router.patch("/{uid}", response_model=NewsItemDTO, operation_id="opensweep_update_news_item")
async def update_news_item(
    uid: str,
    req: UpdateNewsItemRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    service = NewsService()
    n = await service.get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    return await service.update(uid, req, actor_uid=user.uid)


@router.delete("/{uid}", status_code=204, operation_id="opensweep_delete_news_item")
async def delete_news_item(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    n = await NewsService().get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    await NewsService().delete(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/dismiss", response_model=NewsItemDTO, operation_id="opensweep_dismiss_news_item"
)
async def dismiss_news_item(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    n = await NewsService().get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    return await NewsService().dismiss(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/save", response_model=NewsItemDTO, operation_id="opensweep_save_news_item"
)
async def save_news_item(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    n = await NewsService().get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    return await NewsService().save_item(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/convert-to-finding",
    response_model=FindingDTO,
    operation_id="opensweep_convert_news_to_finding",
)
async def convert_news_to_finding(
    uid: str,
    req: ConvertNewsRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
):
    n = await NewsService().get_node(uid)
    await require_repo_in_org(n.repository_uid, user.org_uid)
    return await NewsService().convert_to_finding(
        uid, req or ConvertNewsRequest(), actor_uid=user.uid
    )
