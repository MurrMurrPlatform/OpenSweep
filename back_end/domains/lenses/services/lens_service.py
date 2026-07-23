"""Lens service — list/get/update plus the checklist renderer.

Lenses are platform-level rows (no repository dimension): the seeded set is
shared, and `update` is the org-tuning surface (title/body/tags/enabled
only — structure stays platform-owned, see UpdateLensRequest).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from domains.lenses.models import Lens
from domains.lenses.schemas import LensDTO, UpdateLensRequest
from infrastructure.audit import write_audit


def to_dto(lens: Lens) -> LensDTO:
    return LensDTO(
        uid=lens.uid,
        key=lens.key,
        title=lens.title or "",
        scope=lens.scope or "local",
        body=lens.body or "",
        tags=list(lens.tags or []),
        wants=list(lens.wants or []),
        global_agent_key=lens.global_agent_key or "",
        enabled=bool(lens.enabled),
        provenance=lens.provenance or "system",
        created_at=lens.created_at,
        updated_at=lens.updated_at,
    )


async def list_lenses(*, enabled_only: bool = False) -> list[Lens]:
    """All lenses, stable order (scope then key) so checklists render
    deterministically."""
    nodes = [
        lens
        for lens in await Lens.nodes.all()
        if not enabled_only or lens.enabled
    ]
    nodes.sort(key=lambda lens: (lens.scope or "local", lens.key or ""))
    return nodes


async def get_by_key(key: str) -> Lens:
    lens = await Lens.nodes.get_or_none(key=key)
    if lens is None:
        raise HTTPException(status_code=404, detail=f"Lens {key} not found")
    return lens


async def update(
    key: str, req: UpdateLensRequest, *, actor_uid: str | None = None
) -> LensDTO:
    """Org tuning: title/body/tags/enabled only. The edit is deliberately NOT
    checksum-stamped — a SYNC re-seed detects the drift and preserves it."""
    lens = await get_by_key(key)
    fields = req.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(lens, field, value)
    lens.updated_at = datetime.now(UTC)
    await lens.save()
    await write_audit(
        kind="lens.updated",
        subject_uid=lens.uid,
        subject_type="Lens",
        actor_uid=actor_uid,
        payload={"key": lens.key, "fields": sorted(fields.keys())},
    )
    return to_dto(lens)


DEFAULT_LENSES_BY_KIND: dict[str, tuple[str, ...]] = {
    "subsystem": (
        "bugs", "security", "performance", "error-handling",
        "legacy-patterns", "refactor-opportunities", "simplification", "test-gaps",
    ),
    "feature": ("implementation-gaps",),
    "global": ("architecture-review", "implementation-gaps"),
}


def default_lens_keys(kind: str) -> list[str]:
    return list(DEFAULT_LENSES_BY_KIND.get(kind, ()))


# Closing instruction of every checklist: local runs stay in their lane and
# route cross-cutting observations to the global sweeps via finding tags.
_ESCALATE_INSTRUCTION = (
    "If you notice an issue outside these lenses or outside your scope, do "
    "NOT investigate it — file a brief finding tagged "
    "`escalate:<global-lens-key>` (e.g. escalate:architecture-review) so a "
    "dedicated sweep picks it up."
)


def lens_checklist(lenses: list[Lens]) -> str:
    """Render local lenses into the per-area run checklist. Pure — briefing
    composition calls this with the lenses it already selected."""
    parts = [
        "## Audit lenses for this scope",
        "Work one lens at a time; give a verdict per lens in complete_run "
        "(lens_verdicts).",
        "",
    ]
    for i, lens in enumerate(lenses, start=1):
        parts.append(f"### {i}. {lens.title or lens.key}")
        parts.append((lens.body or "").strip())
        parts.append("")
    parts.append(_ESCALATE_INSTRUCTION)
    return "\n".join(parts)
