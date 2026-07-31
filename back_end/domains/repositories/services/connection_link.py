"""Link a repository to the git credential it should deliver through.

Until now `git_connection_uid` / `github_installation_id` were written once, at
registration, and never again. Credentials outlive that moment: tokens get
rotated, App installations get reinstalled, an org connects a second token for a
different set of repos. `delete_pat_connection` documents that repos "keep their
git_connection_uid" when a connection goes away — which leaves them pointing at
a row that no longer exists, with no supported way to re-point them.

So this module owns the two operations that were missing:

  - `connection_candidates` — every credential in the org a repo could use.
  - `link_repository_connection` — point a repo at one of them, verified.

Linking VERIFIES before it commits. A link that silently records an unusable
credential just moves the failure to the next run, which is the whole problem
this is meant to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from domains.organizations.models import GitConnection
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from logging_config import logger


@dataclass(frozen=True)
class ConnectionCandidate:
    """One credential a repository could be linked to."""

    kind: str  # "pat" | "app"
    uid: str  # GitConnection.uid (both kinds are GitConnection rows)
    account: str  # GitHub login / account the credential belongs to
    installation_id: int | None = None  # kind="app" only
    is_current: bool = False


async def connection_candidates(repo: Any, org_uid: str) -> list[ConnectionCandidate]:
    """Every git credential in `org_uid`, flagged with which one `repo` uses.

    Both kinds live in GitConnection: kind="pat" carries a sealed token,
    kind="app" carries an installation id in `external_id`.
    """
    current_uid = str(getattr(repo, "git_connection_uid", "") or "")
    current_installation = getattr(repo, "github_installation_id", None)

    out: list[ConnectionCandidate] = []
    for conn in await GitConnection.nodes.filter(org_uid=org_uid):
        kind = conn.kind or "app"  # pre-`kind` rows are App installations
        installation_id: int | None = None
        if kind == "app":
            try:
                installation_id = int(conn.external_id)
            except (TypeError, ValueError):
                # A malformed external_id can't be linked to; skip rather than
                # offer a candidate that would fail on selection.
                continue
        out.append(
            ConnectionCandidate(
                kind=kind,
                uid=conn.uid,
                account=conn.display_name or "",
                installation_id=installation_id,
                is_current=(
                    conn.uid == current_uid
                    if kind == "pat"
                    else installation_id is not None
                    and installation_id == current_installation
                ),
            )
        )
    out.sort(key=lambda c: (not c.is_current, c.kind, c.account.lower()))
    return out


async def link_repository_connection(
    repo: Repository, *, connection_uid: str, org_uid: str, actor_uid: str = ""
) -> Repository:
    """Point `repo` at one of its org's connections, after checking it works.

    Tenancy: the connection must belong to the SAME org as the repo — without
    that check this endpoint would let anyone borrow another tenant's
    credential by uid.
    """
    conn = await GitConnection.nodes.get_or_none(uid=connection_uid, org_uid=org_uid)
    if conn is None:
        raise HTTPException(
            status_code=404, detail=f"git connection {connection_uid} not found in this org"
        )

    kind = conn.kind or "app"
    previous = {
        "git_connection_uid": repo.git_connection_uid,
        "github_installation_id": repo.github_installation_id,
    }

    if kind == "pat":
        repo.git_connection_uid = conn.uid
        # A PAT and an installation are alternative credentials, not additive:
        # `get_repo_git_token` prefers the installation whenever one is set, so
        # leaving a stale id here would silently ignore the token just chosen.
        repo.github_installation_id = None
    else:
        try:
            repo.github_installation_id = int(conn.external_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"connection {connection_uid} has no usable installation id",
            ) from exc
        repo.git_connection_uid = conn.uid

    repo.updated_at = datetime.now(UTC)
    await repo.save()

    # Verify AFTER pointing at it — resolution reads the repo node, so the new
    # credential has to be in place for the check to describe reality. On a bad
    # link we roll back rather than leave the repo worse than we found it.
    from domains.delivery.services.write_gate import evaluate_write_access

    access = await evaluate_write_access(repo)
    if access.state == "disconnected":
        repo.git_connection_uid = previous["git_connection_uid"]
        repo.github_installation_id = previous["github_installation_id"]
        await repo.save()
        raise HTTPException(status_code=422, detail=access.reason)

    await write_audit(
        kind="repository.connection_linked",
        subject_uid=repo.uid,
        subject_type="Repository",
        actor_uid=actor_uid or "operator",
        payload={
            "connection_uid": conn.uid,
            "kind": kind,
            "account": conn.display_name or "",
            "previous": previous,
            "write_access": access.state,
        },
    )
    logger.info(
        f"repository {repo.slug} linked to {kind} connection {conn.uid} "
        f"(write access: {access.state})",
        extra={"tag": "repositories"},
    )
    return repo
