"""Repair agent-authored text that was stored JSON-escaped instead of decoded.

Backfill for the bug fixed in `domains/platform_tools/text_repair.py`: models
that relayed a document through a subagent report sent the *escaped* form of
their markdown (one physical line, literal `\\n`, `\\#` headings), and the
platform stored it verbatim. New writes are repaired at the ingestion
boundary; this repairs what already landed.

Only agent-authored prose fields are considered — never a serialized JSON blob
(`Run.usage`, `Analysis.sections`, `Ticket.plan`, `Thread.events`), whose
backslash escapes are correct encoding. `looks_escaped` skips those anyway;
the explicit field list is the second lock.

Idempotent: repaired text has real newlines, so a second pass matches nothing.

    docker exec opensweep_backend python -m scripts.repair_escaped_text --dry-run
    docker exec opensweep_backend python -m scripts.repair_escaped_text
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# (label, property) pairs holding agent-authored markdown/prose.
TARGETS: list[tuple[str, str]] = [
    ("Doc", "body"),
    ("DocEdit", "proposed_body"),
    ("Finding", "description"),
    ("Finding", "suggested_fix"),
    ("Finding", "why_it_matters"),
    ("Ticket", "description"),
    ("Memory", "body"),
    ("Area", "spec"),
    ("AreaEdit", "proposed_spec"),
]


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from infrastructure.neomodel_config import configure_neomodel
    from neomodel import adb
    from neomodel import config as neomodel_conf

    configure_neomodel()
    if not adb.driver:
        await adb.set_connection(url=neomodel_conf.DATABASE_URL)

    from domains.platform_tools.text_repair import looks_escaped, repair_text

    dry_run = "--dry-run" in sys.argv
    total = 0
    for label, prop in TARGETS:
        rows, _ = await adb.cypher_query(
            f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL RETURN n.uid, n.{prop}"
        )
        repaired = [(uid, repair_text(v)) for uid, v in rows if looks_escaped(v or "")]
        if not repaired:
            continue
        total += len(repaired)
        print(f"{label}.{prop}: {len(repaired)} escaped")
        if dry_run:
            continue
        await adb.cypher_query(
            f"UNWIND $rows AS row MATCH (n:{label} {{uid: row.uid}}) SET n.{prop} = row.value",
            {"rows": [{"uid": uid, "value": value} for uid, value in repaired]},
        )
    verb = "would repair" if dry_run else "repaired"
    print(f"{verb} {total} field(s)")


if __name__ == "__main__":
    asyncio.run(main())
