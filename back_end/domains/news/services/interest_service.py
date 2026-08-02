"""Interest service — the user-entered topics the news scout watches."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.news.models import Interest
from domains.news.schemas import (
    CreateInterestRequest,
    InterestDTO,
    UpdateInterestRequest,
)
from infrastructure.audit import write_audit


def interest_to_dto(i: Interest) -> InterestDTO:
    return InterestDTO(
        uid=i.uid,
        repository_uid=i.repository_uid,
        title=i.title,
        details=i.details or "",
        enabled=bool(i.enabled),
        created_at=i.created_at,
        updated_at=i.updated_at,
    )


class InterestService:
    async def list(
        self, *, repository_uids: list[str], enabled_only: bool = False
    ) -> list[InterestDTO]:
        """Interests in ``repository_uids`` — the caller's tenancy scope, so an
        empty list means an empty result, never "every interest"."""
        if not repository_uids:
            return []
        out = []
        for i in await Interest.nodes.filter(repository_uid__in=repository_uids):
            if enabled_only and not i.enabled:
                continue
            out.append(interest_to_dto(i))
        out.sort(
            key=lambda i: i.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return out

    async def get_node(self, uid: str) -> Interest:
        i = await Interest.nodes.get_or_none(uid=uid)
        if i is None:
            raise HTTPException(status_code=404, detail=f"Interest {uid} not found")
        return i

    async def create(
        self, req: CreateInterestRequest, *, actor_uid: str | None = None
    ) -> InterestDTO:
        i = Interest(
            uid=uuid4().hex,
            repository_uid=req.repository_uid,
            title=req.title,
            details=req.details,
            enabled=req.enabled,
        )
        await i.save()
        await write_audit(
            kind="interest.created",
            subject_uid=i.uid,
            subject_type="Interest",
            actor_uid=actor_uid,
            repository_uid=i.repository_uid,
        )
        return interest_to_dto(i)

    async def update(
        self, uid: str, req: UpdateInterestRequest, *, actor_uid: str | None = None
    ) -> InterestDTO:
        i = await self.get_node(uid)
        fields = req.model_dump(exclude_unset=True)
        for key, value in fields.items():
            setattr(i, key, value)
        i.updated_at = datetime.now(UTC)
        await i.save()
        await write_audit(
            kind="interest.edited",
            subject_uid=uid,
            subject_type="Interest",
            actor_uid=actor_uid,
            repository_uid=i.repository_uid,
            payload={"fields": sorted(fields.keys())},
        )
        return interest_to_dto(i)

    async def delete(self, uid: str, *, actor_uid: str | None = None) -> None:
        i = await self.get_node(uid)
        repository_uid = i.repository_uid
        await i.delete()
        await write_audit(
            kind="interest.deleted",
            subject_uid=uid,
            subject_type="Interest",
            actor_uid=actor_uid,
            repository_uid=repository_uid,
        )
