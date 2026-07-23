"""Lens DTOs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class LensDTO(BaseModel):
    uid: str
    key: str
    title: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    wants: list[str] = Field(default_factory=list)
    global_agent_key: str = ""
    enabled: bool = True
    provenance: str = "system"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UpdateLensRequest(BaseModel):
    """Org tuning of a lens. Structure (key, global_agent_key, wants)
    stays platform-owned; the tunable surface is the prose and its labels."""

    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None
    enabled: Optional[bool] = None
