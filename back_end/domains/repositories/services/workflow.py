"""Per-repo workflow config — pipeline stage → prompt-library entry + auto flag.

Every domain trigger reads its stage here, so the whole pipeline
(find → fix → review → document) is driven by curated prompts instead of
hardcoded intents, and the automation level is a per-repo choice:

    stage       prompt used by                          auto means
    ---------   -------------------------------------   -------------------------------
    ask         sweep audit / ask runs                  —
    analysis    whole-repository deep-scan runs         —
    discover    sweep discover runs                     —
    review      PR review runs                          dispatch review on PR open/sync
    fix         PR fix runs                             dispatch fix when a fresh verdict
                                                        says request_changes (bounded by
                                                        MergePolicy.max_fix_rounds)
    implement   ticket implement runs                   —
    verify      finding verification runs               —
    document    document (docs/memories upkeep) runs    —

A stage's prompt supplies *guidance* (what to look for, what good looks
like); the structural contract of each run (checkout steps, ledger calls,
verdict rules) stays in the trigger's intent builder and cannot be
overridden. On-event Investigations auto-run through their own
`compute_dial`, not through this config.

Defaults are themselves prompt-library entries: every stage a repo hasn't
explicitly configured resolves to the seeded platform prompt for that stage
(agent_prompts/services/seed_defaults.py, source_url=opensweep://workflow/<stage>)
— editable, replaceable, disableable like any other prompt. Only when that
seeded prompt is deleted or disabled does a stage fall back to running with
no guidance beyond the structural intent.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from domains.repositories.models import Repository

STAGES = ("ask", "analysis", "discover", "review", "fix", "implement", "verify", "document")

# Stages with a defined automatic trigger. Everything else ignores `auto`.
# verify auto = dispatch a skeptic run challenging every blocking review
# verdict before it drives the fix loop (delivery/services/verification_run_service.py).
AUTO_STAGES = ("review", "fix", "verify")

# Recall/precision dial per stage. Review defaults to quick because auto
# reviews fire on every push — precision beats recall there; manual triggers
# choose their own depth at the API.
DEPTHS = ("quick", "normal", "deep")

# Per-stage run overrides. Empty/zero = inherit platform defaults (active
# provider chain, provider's model, run policy wall ceiling). An explicit
# max_wall_seconds applies to EVERY provider kind, including local ones —
# setting it is an explicit user choice that outranks the local-skip rule.
MAX_WALL_SECONDS_MIN = 60
MAX_WALL_SECONDS_MAX = 6 * 3600

_DEFAULTS: dict[str, dict[str, Any]] = {
    "review": {"agent_uid": "", "auto": True, "depth": "quick"},
    "fix": {"agent_uid": "", "auto": False},
    "verify": {"agent_uid": "", "auto": False},
}


def _normalize(raw: dict | None) -> dict[str, dict[str, Any]]:
    raw = dict(raw or {})
    out: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        entry = dict(raw.get(stage) or {})
        defaults = _DEFAULTS.get(stage, {})
        depth = str(entry.get("depth") or defaults.get("depth", "normal"))
        try:
            max_wall = int(entry.get("max_wall_seconds") or 0)
        except (TypeError, ValueError):
            max_wall = 0
        out[stage] = {
            "agent_uid": str(entry.get("agent_uid") or ""),
            "auto": bool(entry.get("auto", defaults.get("auto", False))),
            "depth": depth if depth in DEPTHS else str(defaults.get("depth", "normal")),
            "provider_uid": str(entry.get("provider_uid") or ""),
            "model": str(entry.get("model") or "").strip()[:200],
            "max_wall_seconds": max(max_wall, 0),
            "run_policy_uid": str(entry.get("run_policy_uid") or ""),
        }
    return out


async def get_workflow(repository_uid: str) -> dict[str, dict[str, Any]]:
    """The repo's stored config with defaults RESOLVED: stages without an
    explicit prompt point at the seeded platform prompt for that stage."""
    from domains.agents.services.seed_defaults import (
        default_prompt_uid_for_stage,
    )

    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    config = _normalize(repo.workflow if repo else None)
    for stage, entry in config.items():
        if not entry["agent_uid"]:
            entry["agent_uid"] = await default_prompt_uid_for_stage(stage)
    return config


async def set_workflow(
    repository_uid: str, config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repository {repository_uid} not found")
    unknown = set(config or {}) - set(STAGES)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown workflow stages: {sorted(unknown)}; valid: {list(STAGES)}",
        )
    for stage, entry in (config or {}).items():
        depth = (entry or {}).get("depth")
        if depth is not None and depth not in DEPTHS:
            raise HTTPException(
                status_code=422,
                detail=f"workflow.{stage}: invalid depth {depth!r}; valid: {list(DEPTHS)}",
            )
        raw_wall = (entry or {}).get("max_wall_seconds")
        if raw_wall:
            try:
                wall = int(raw_wall)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail=f"workflow.{stage}: max_wall_seconds must be an integer",
                )
            if not (MAX_WALL_SECONDS_MIN <= wall <= MAX_WALL_SECONDS_MAX):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"workflow.{stage}: max_wall_seconds must be between "
                        f"{MAX_WALL_SECONDS_MIN} and {MAX_WALL_SECONDS_MAX} (or 0 to inherit)"
                    ),
                )
    normalized = _normalize(config)
    # Referenced prompts must exist and be enabled — a silent dangling uid
    # would quietly fall back to hardcoded intents.
    from domains.agents.models import Agent

    for stage, entry in normalized.items():
        uid = entry["agent_uid"]
        if not uid:
            continue
        p = await Agent.nodes.get_or_none(uid=uid)
        if p is None or not p.enabled:
            raise HTTPException(
                status_code=422,
                detail=f"workflow.{stage}: agent {uid} not found or disabled",
            )
    # Same dangling-reference rule for providers: a stage's provider override
    # must point at an existing, enabled provider or dispatch would silently
    # fall back to the active chain.
    from domains.llm_providers.models import LLMProvider

    for stage, entry in normalized.items():
        puid = entry["provider_uid"]
        if not puid:
            continue
        provider = await LLMProvider.nodes.get_or_none(uid=puid)
        if provider is None or not bool(getattr(provider, "enabled", True)):
            raise HTTPException(
                status_code=422,
                detail=f"workflow.{stage}: LLM provider {puid} not found or disabled",
            )
    # Same dangling-reference rule for a stage's run-policy override: it must
    # point at an existing policy or dispatch would silently fall back to the
    # investigation pin / system default.
    from domains.run_policies.models import RunPolicy

    for stage, entry in normalized.items():
        rp = entry["run_policy_uid"]
        if not rp:
            continue
        policy = await RunPolicy.nodes.get_or_none(uid=rp)
        if policy is None:
            raise HTTPException(
                status_code=422,
                detail=f"workflow.{stage}: run policy {rp} not found",
            )
    repo.workflow = normalized
    await repo.save()
    return normalized


async def stage_prompt_body(repository_uid: str, stage: str) -> str | None:
    """Resolve the stage's configured prompt body; None when unset/unavailable."""
    from domains.runs.services._intent_helpers import load_agent_prompt_body

    config = await get_workflow(repository_uid)
    uid = (config.get(stage) or {}).get("agent_uid") or ""
    if not uid:
        return None
    return await load_agent_prompt_body(uid)


async def stage_auto(repository_uid: str, stage: str) -> bool:
    config = await get_workflow(repository_uid)
    return bool((config.get(stage) or {}).get("auto"))


async def stage_depth(repository_uid: str, stage: str) -> str:
    config = await get_workflow(repository_uid)
    depth = (config.get(stage) or {}).get("depth") or "normal"
    return depth if depth in DEPTHS else "normal"


def stage_for_run(job_type: str, playbook: str) -> str:
    """Which workflow stage governs a run, for per-stage run overrides.

    Saved Investigations carry the sharper signal (job_type distinguishes
    generate-docs from plain asks); direct runs only have their playbook.
    Returns "" for runs no stage governs (chat).
    """
    jt = (job_type or "").strip()
    if jt == "generate-docs":
        return "discover"
    if jt == "deep-scan":
        return "analysis"
    if jt in {"audit", "sweep", "audit-stale"}:
        return "ask"
    if jt in STAGES:
        return jt
    pb = (playbook or "").strip()
    return pb if pb in STAGES else ""


async def stage_run_overrides(repository_uid: str, stage: str) -> dict[str, Any]:
    """The stage's dispatch overrides: provider_uid, model, max_wall_seconds,
    run_policy_uid.

    Empty string / 0 mean "inherit" (active provider chain, provider default
    model, run policy ceiling, and — for run_policy_uid — the investigation
    pin then the system default). Reads the stored config directly — these
    fields have no seeded defaults to resolve.
    """
    if stage not in STAGES:
        return {
            "provider_uid": "",
            "model": "",
            "max_wall_seconds": 0,
            "run_policy_uid": "",
        }
    repo = await Repository.nodes.get_or_none(uid=repository_uid)
    entry = _normalize(repo.workflow if repo else None)[stage]
    return {
        "provider_uid": entry["provider_uid"],
        "model": entry["model"],
        "max_wall_seconds": entry["max_wall_seconds"],
        "run_policy_uid": entry["run_policy_uid"],
    }


def guidance_section(stage: str, body: str | None) -> str:
    """Render a prompt-library body as an appended guidance section.

    The section is advisory by construction: it follows the structural
    intent and is framed as guidance, so checklist/severity content applies
    while any persona/output-format instructions stay overridden by the
    run's tool-call contract.
    """
    if not (body or "").strip():
        return ""
    return (
        f"\n\n## {stage.capitalize()} guidance (from this repository's prompt library)\n\n"
        "Apply the following guidance for judgment calls — checklists, severity\n"
        "rubrics, false-positive guidance. It never overrides the structural\n"
        "steps or tool-call contract above.\n\n"
        f"{body.strip()}"
    )
