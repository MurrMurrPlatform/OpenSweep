"""One-shot Linear → OpenSweep ticket import (PLATFORM_V2_DESIGN.md §15 Phase 2).

Reads a Linear team's issues over GraphQL and creates OpenSweep Tickets:

  - open issues (state type `backlog` / `unstarted`)  → status `backlog`
  - started issues (state type `started`)             → status `todo`
  - canceled issues (closed-as-canceled / wont-fix)   → status `done`,
    labeled `wont-fix`, so the history survives the Linear shutdown
  - completed issues and `triage`-type states are skipped (completed work
    needs no ticket; un-triaged intake should be re-triaged in OpenSweep)

Every imported ticket keeps a provenance footer with the Linear identifier
and URL; re-running the script skips issues whose identifier already appears
in an existing ticket description (idempotent).

DOCUMENTED DEVIATION from the design's "won't-fix history into waiver
suppressions": Linear issues do not map to OpenSweep finding dedupe keys (they
were never findings), so waiver suppression *by key* is not possible. The
closest faithful representation is a `done` ticket labeled `wont-fix` — human
reviewers and agents can search these before re-proposing work, but automatic
re-discovery suppression only applies to findings waived inside OpenSweep.

Usage (inside back_end/, with LINEAR_API_KEY exported):

    uv run python scripts/linear_import.py --repository-uid <uid> [--dry-run]

Env:
    LINEAR_API_KEY   Linear personal API key (sent as `Authorization: <key>`)
    LINEAR_TEAM_KEY  Linear team key to import (default "MUR")
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Linear priority: 0 = none, 1 = urgent, 2 = high, 3 = medium, 4 = low.
_PRIORITY_BY_NUMBER = {0: "medium", 1: "urgent", 2: "high", 3: "medium", 4: "low"}
_PRIORITY_BY_NAME = {
    "no priority": "medium",
    "none": "medium",
    "urgent": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

_ISSUES_QUERY = """
query Issues($teamKey: String!, $after: String) {
  issues(
    first: 100
    after: $after
    filter: { team: { key: { eq: $teamKey } } }
    includeArchived: true
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      description
      url
      priority
      state { name type }
      labels { nodes { name } }
    }
  }
}
"""


# ── Pure mapping functions (unit-tested, no network) ─────────────────────────


def map_priority(value: Any) -> str:
    """Linear priority (0-4 int or label string) → OpenSweep ticket priority."""
    if isinstance(value, bool):  # bools are ints; reject explicitly
        return "medium"
    if isinstance(value, int):
        return _PRIORITY_BY_NUMBER.get(value, "medium")
    if isinstance(value, str):
        return _PRIORITY_BY_NAME.get(value.strip().lower(), "medium")
    return "medium"


def map_state_type(state_type: str) -> str | None:
    """Linear workflow-state type → OpenSweep ticket status, or None to skip.

    backlog/unstarted → backlog; started → todo; canceled → done (wont-fix);
    completed/triage/unknown → skip.
    """
    return {
        "backlog": "backlog",
        "unstarted": "backlog",
        "started": "todo",
        "canceled": "done",
    }.get((state_type or "").strip().lower())


def map_labels(label_names: list[str], state_type: str) -> list[str]:
    """Linear labels, plus `wont-fix` for canceled issues (deduped, ordered)."""
    labels = [name for name in label_names if name]
    if (state_type or "").strip().lower() == "canceled" and "wont-fix" not in labels:
        labels.append("wont-fix")
    return list(dict.fromkeys(labels))


def build_description(description: str, identifier: str, url: str) -> str:
    """Linear description + a provenance footer (also the idempotency marker)."""
    footer = f"---\nImported from Linear issue `{identifier}`"
    if url:
        footer += f" ({url})"
    footer += "."
    body = (description or "").rstrip()
    return f"{body}\n\n{footer}" if body else footer


def is_already_imported(identifier: str, existing_descriptions: list[str]) -> bool:
    # Match the exact backtick-delimited token build_description() writes into
    # the footer, not a bare substring: Linear identifiers are TEAM-N with
    # unbounded N, so every identifier is a prefix of infinitely many longer
    # ones sharing its team (MUR-1 ⊂ MUR-11 ⊂ MUR-100 …). A bare substring
    # check would silently mark the shorter one "already imported" once the
    # longer one landed.
    if not identifier:
        return False
    marker = f"`{identifier}`"
    return any(marker in (d or "") for d in existing_descriptions)


def issue_to_ticket_fields(issue: dict, repository_uid: str) -> dict | None:
    """Map one Linear issue payload to Ticket constructor kwargs (None = skip)."""
    state_type = ((issue.get("state") or {}).get("type")) or ""
    status = map_state_type(state_type)
    if status is None:
        return None
    label_names = [n.get("name") or "" for n in ((issue.get("labels") or {}).get("nodes") or [])]
    return {
        "repository_uid": repository_uid,
        "title": issue.get("title") or issue.get("identifier") or "(untitled)",
        "description": build_description(
            issue.get("description") or "", issue.get("identifier") or "", issue.get("url") or ""
        ),
        "labels": map_labels(label_names, state_type),
        "status": status,
        "priority": map_priority(issue.get("priority")),
        "origin": "human",
    }


# ── Network + persistence (not unit-tested) ──────────────────────────────────


async def fetch_issues(api_key: str, team_key: str) -> list[dict]:
    import httpx

    issues: list[dict] = []
    after: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.post(
                LINEAR_GRAPHQL_URL,
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={"query": _ISSUES_QUERY, "variables": {"teamKey": team_key, "after": after}},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(f"Linear GraphQL errors: {data['errors']}")
            page = data["data"]["issues"]
            issues.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return issues
            after = page["pageInfo"]["endCursor"]


async def run_import(repository_uid: str, team_key: str, *, dry_run: bool) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from neomodel import adb
    from neomodel import config as neomodel_conf

    from infrastructure.neomodel_config import configure_neomodel

    configure_neomodel()
    if not adb.driver:
        await adb.set_connection(url=neomodel_conf.DATABASE_URL)

    from datetime import UTC, datetime
    from uuid import uuid4

    from domains.tickets.models import Ticket

    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        print("LINEAR_API_KEY is not set — aborting.", file=sys.stderr)
        raise SystemExit(1)

    issues = await fetch_issues(api_key, team_key)
    existing = await Ticket.nodes.filter(repository_uid=repository_uid)
    existing_descriptions = [t.description or "" for t in existing]

    rows: list[tuple[str, str, str, str]] = []  # identifier, title, status, action
    created = skipped_dup = skipped_state = 0

    for issue in issues:
        identifier = issue.get("identifier") or "?"
        title = (issue.get("title") or "")[:60]
        fields = issue_to_ticket_fields(issue, repository_uid)
        if fields is None:
            state_type = ((issue.get("state") or {}).get("type")) or "?"
            rows.append((identifier, title, "-", f"skipped ({state_type})"))
            skipped_state += 1
            continue
        if is_already_imported(identifier, existing_descriptions):
            rows.append((identifier, title, fields["status"], "skipped (already imported)"))
            skipped_dup += 1
            continue
        if dry_run:
            rows.append((identifier, title, fields["status"], "would create"))
            created += 1
            continue
        ticket = Ticket(uid=uuid4().hex, **fields)
        if fields["status"] == "done":
            ticket.done_at = datetime.now(UTC)
        await ticket.save()
        existing_descriptions.append(ticket.description)
        rows.append((identifier, title, fields["status"], "created"))
        created += 1

    header = ("LINEAR", "TITLE", "STATUS", "ACTION")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(4)]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    verb = "would create" if dry_run else "created"
    print(
        f"\n{verb}: {created} | already imported: {skipped_dup} | "
        f"skipped by state: {skipped_state} | total from Linear: {len(issues)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot Linear → OpenSweep ticket import")
    parser.add_argument("--repository-uid", required=True, help="OpenSweep Repository uid to file tickets under")
    parser.add_argument(
        "--team-key",
        default=os.environ.get("LINEAR_TEAM_KEY", "MUR"),
        help="Linear team key (default: LINEAR_TEAM_KEY env or 'MUR')",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    args = parser.parse_args()
    asyncio.run(run_import(args.repository_uid, args.team_key, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
