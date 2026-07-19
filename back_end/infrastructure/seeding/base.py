"""Seeding primitives — mode, result, and the content-hash helper.

The seeders (infrastructure/seeding/registry.py) install the rows a fresh
OpenSweep needs: the system RunPolicy, the prompt library, per-repo docs and
ScheduledAgent bindings. Every seeder is idempotent; what differs is how it treats a
row that already exists, and that is the SeedMode.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class SeedMode(str, Enum):
    """How a seeder treats a row that already exists.

    UPSERT — create-if-absent, never touch an existing row. The safe minimum:
        a re-seed can only add what's missing. (Historic behavior.)

    SYNC — create-if-absent, and refresh platform-owned rows the user has NOT
        edited to the currently shipped content, detected via the stored
        content hash (see AgentPrompt.seed_checksum). A row the user edited in
        place is preserved. This is the deploy default: improved shipped
        defaults roll forward, hand-tuned rows are left alone.

    FORCE — create-if-absent, and overwrite EVERY platform-owned row to the
        shipped content, discarding user edits to platform rows. The operator's
        deliberate "reset the defaults" escape hatch; never the default.
    """

    UPSERT = "upsert"
    SYNC = "sync"
    FORCE = "force"


@dataclass
class SeedResult:
    """What one seeder did. Counts are best-effort and purely for logging."""

    name: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    preserved: int = 0  # existing rows left untouched because the user owns them
    note: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict:
        d = {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "preserved": self.preserved,
        }
        if self.note:
            d["note"] = self.note
        if self.error:
            d["error"] = self.error
        return d


def content_hash(*parts: object) -> str:
    """Stable hash of a row's platform-owned content. Order matters; a NUL
    separator keeps ("a", "bc") distinct from ("ab", "c")."""
    payload = "\x00".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode()).hexdigest()
