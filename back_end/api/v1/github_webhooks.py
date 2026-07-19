"""GitHub webhook receiver — signature-verified, idempotent, head-driven (§7).

Events never mutate state directly. A relevant event only names *which PR*
to re-sync; the sync then re-reads head state from the GitHub API and
recomputes the convergence predicate. Replayed deliveries are no-ops.

Setup — two paths:
  - GitHub App (auto-connect, preferred): the manifest flow registers this
    URL as the App's hook; installs then deliver `installation` /
    `installation_repositories` events here. Those events do NOT create
    Repository nodes — repos are registered by explicit selection
    (api/v1/github_app.py: available-repos → register-repo). The events only
    LINK the installation onto already-registered matching repos (and unlink
    on removal/uninstall), and audit how many repos became available.
    Signatures verify against the App's manifest-issued webhook secret.
  - Manual repo webhook (legacy/migration): repo → Settings → Webhooks,
    Payload URL {OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL}/api/v1/github/webhook,
    content type application/json, secret GITHUB_WEBHOOK_SECRET, events:
    pull requests, check suites, check runs, pushes.

Both secrets are accepted concurrently (App first, env fallback) so a
migration can proceed webhook-by-webhook without a flag day.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from neomodel import adb

from config import settings
from domains.delivery.models import PullRequest, WebhookDelivery, pr_key
from domains.delivery.services.pull_request_service import PullRequestService
from domains.organizations.models import GitConnection
from domains.repositories.models import Repository
from infrastructure.audit import write_audit
from infrastructure.github_app_store import get_github_app
from infrastructure.github_webhook import verify_signature
from logging_config import logger

router = APIRouter(prefix="/api/v1/github", tags=["github-webhooks"])

_HANDLED_EVENTS = {
    "pull_request",
    "check_suite",
    "check_run",
    "push",
    "installation",
    "installation_repositories",
}

# A delivery stuck in `processing` longer than this is presumed orphaned
# (worker died mid-flight) and is reprocessed on redelivery.
STALE_PROCESSING_AFTER = timedelta(minutes=5)


def delivery_disposition(*, status: str | None, updated_at: datetime | None, now: datetime) -> str:
    """Decide what to do with an incoming delivery id — pure, unit-testable.

    Returns:
      - "new"       — never seen; process it.
      - "duplicate" — already succeeded, or still in-flight (< stale window);
                       no-op so replays/racing redeliveries don't double-run.
      - "reprocess" — failed earlier, or processing but stale: run it again
                       (head-driven sync makes reprocessing idempotent).
    """
    if status is None:
        return "new"
    if status == "succeeded":
        return "duplicate"
    if status == "processing":
        if updated_at is not None and (now - updated_at) < STALE_PROCESSING_AFTER:
            return "duplicate"
        return "reprocess"
    return "reprocess"  # failed


async def _claim_delivery(*, delivery_id: str, event: str, action: str, now: datetime) -> bool:
    """Atomically claim a delivery id for processing — True when THIS call owns it.

    One Cypher round-trip so two racing identical deliveries cannot both
    claim: MERGE (backed by the unique constraint on delivery_id) creates or
    matches, a per-call marker distinguishes creation, and the re-claim rules
    from `delivery_disposition` (failed, or processing gone stale) apply in
    the same statement. DateTimeProperty lands in the store as a numeric
    epoch, hence the timestamp comparisons.
    """
    marker = uuid4().hex
    rows, _ = await adb.cypher_query(
        """
        MERGE (d:WebhookDelivery {delivery_id: $id})
        ON CREATE SET d.event = $event, d.action = $action,
                      d.status = 'processing', d.attempts = 1,
                      d.received_at = $now, d.updated_at = $now,
                      d.claim_marker = $marker
        WITH d, coalesce(d.claim_marker = $marker, false) AS created
        REMOVE d.claim_marker
        WITH d, created,
             (NOT created AND (
                 NOT coalesce(d.status, 'succeeded') IN ['succeeded', 'processing']
                 OR (d.status = 'processing'
                     AND coalesce(d.updated_at, d.received_at, 0) < $stale_before)
             )) AS reclaim
        FOREACH (_ IN CASE WHEN reclaim THEN [1] ELSE [] END |
            SET d.status = 'processing',
                d.attempts = coalesce(d.attempts, 0) + 1,
                d.event = $event, d.action = $action,
                d.updated_at = $now)
        RETURN created OR reclaim
        """,
        {
            "id": delivery_id,
            "event": event,
            "action": action,
            "now": now.timestamp(),
            "stale_before": (now - STALE_PROCESSING_AFTER).timestamp(),
            "marker": marker,
        },
    )
    return bool(rows and rows[0][0])


async def _registered_repos(*, repo_id, owner: str, name: str) -> list[Repository]:
    """ALL Repository nodes for one GitHub repo — the same repo may be
    registered by multiple orgs (one node per org). Matched by
    github_repo_id first, then (owner, name); deduped by uid."""
    nodes: list[Repository] = []
    if repo_id is not None:
        nodes = list(await Repository.nodes.filter(github_repo_id=int(repo_id)))
    if owner and name:
        seen = {n.uid for n in nodes}
        # Name fallback covers nodes registered without a repo id — a node
        # carrying a DIFFERENT id is another repo that happens to share the
        # name (e.g. after a rename) and is skipped.
        nodes.extend(
            n
            for n in await Repository.nodes.filter(github_owner=owner, github_repo=name)
            if n.uid not in seen
            and (repo_id is None or n.github_repo_id in (None, int(repo_id)))
        )
    return nodes


async def _repos_for_payload(payload: dict) -> list[Repository]:
    gh_repo = payload.get("repository") or {}
    return await _registered_repos(
        repo_id=gh_repo.get("id"),
        owner=(gh_repo.get("owner") or {}).get("login") or "",
        name=gh_repo.get("name") or "",
    )


async def _pr_numbers_to_sync(event: str, payload: dict, repo: Repository) -> list[int]:
    """Head-driven dispatch: derive which PR numbers this event touches."""
    if event == "pull_request":
        number = (payload.get("pull_request") or {}).get("number") or payload.get("number")
        return [int(number)] if number else []

    if event in {"check_suite", "check_run"}:
        obj = payload.get(event) or {}
        # check_run nests its suite; both carry pull_requests + head_sha.
        prs = obj.get("pull_requests") or (obj.get("check_suite") or {}).get("pull_requests") or []
        numbers = [int(p["number"]) for p in prs if p.get("number")]
        if numbers:
            return numbers
        # Fork PRs arrive with an empty pull_requests array — fall back to
        # matching stored open PRs by head sha.
        head_sha = obj.get("head_sha") or (obj.get("check_suite") or {}).get("head_sha") or ""
        if head_sha:
            nodes = await PullRequest.nodes.filter(
                repository_uid=repo.uid, head_sha=head_sha, state="open"
            )
            return [int(pr.github_number) for pr in nodes]
        return []

    if event == "push":
        # A push to a branch that is the head of an open PR (e.g. a fix commit)
        # re-syncs that PR; pull_request.synchronize usually covers this, but
        # pushes via other tokens can suppress that event.
        ref = payload.get("ref") or ""
        branch = ref.removeprefix("refs/heads/")
        if not branch:
            return []
        nodes = await PullRequest.nodes.filter(
            repository_uid=repo.uid, head_ref=branch, state="open"
        )
        return [int(pr.github_number) for pr in nodes]

    return []


def _webhook_secrets() -> list[str]:
    """Accepted HMAC secrets, in trust order: the connected App's
    manifest-issued secret first, then the env secret (manual repo webhooks
    and the migration window). Empty when neither is configured — verification
    then fails closed."""
    secrets: list[str] = []
    app = get_github_app()
    if app is not None and app.webhook_secret:
        secrets.append(app.webhook_secret)
    if settings.GITHUB_WEBHOOK_SECRET and settings.GITHUB_WEBHOOK_SECRET not in secrets:
        secrets.append(settings.GITHUB_WEBHOOK_SECRET)
    return secrets


@router.post("/webhook", operation_id="opensweep_github_webhook")
async def github_webhook(request: Request) -> dict:
    body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    # `or [""]`: with nothing configured we still run one verification with an
    # empty secret, which fails closed inside verify_signature (and keeps
    # verify_signature the single monkeypatchable trust seam in tests).
    if not any(
        verify_signature(secret=secret, body=body, signature_header=signature_header)
        for secret in (_webhook_secrets() or [""])
    ):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if event == "ping":
        return {"ok": True, "event": "ping"}
    if event not in _HANDLED_EVENTS:
        return {"ok": True, "event": event, "handled": False}
    if not delivery_id:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")

    # Idempotency: a delivery id is processed successfully exactly once.
    # Failed (or stale in-flight) deliveries are reprocessed on redelivery —
    # a failed "PR merged" event must not be permanently dropped. The claim
    # itself is a single atomic Cypher statement so two racing identical
    # deliveries cannot both process.
    now = datetime.now(UTC)
    payload = await request.json()
    action = payload.get("action") or ""
    if not await _claim_delivery(delivery_id=delivery_id, event=event, action=action, now=now):
        return {"ok": True, "event": event, "duplicate": True}

    # `succeeded` is only recorded after the handler body completes; any
    # exception marks the delivery failed and returns 500 so GitHub retries.
    try:
        result = await _process_delivery(event=event, action=action, payload=payload)
    except Exception as exc:
        delivery = await WebhookDelivery.nodes.get_or_none(delivery_id=delivery_id)
        attempts = int(delivery.attempts or 0) if delivery is not None else 0
        if delivery is not None:
            delivery.status = "failed"
            delivery.updated_at = datetime.now(UTC)
            await delivery.save()
        logger.warning(
            f"webhook {event}/{action} delivery {delivery_id} failed "
            f"(attempt {attempts}): {exc}",
            extra={"tag": "delivery"},
        )
        raise HTTPException(
            status_code=500, detail="webhook processing failed — GitHub will redeliver"
        ) from exc

    delivery = await WebhookDelivery.nodes.get_or_none(delivery_id=delivery_id)
    if delivery is not None:
        delivery.status = "succeeded"
        delivery.updated_at = datetime.now(UTC)
        await delivery.save()
    return result


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ── GitHub App installation events — link/unlink registered repos (§7) ──────


@dataclass
class InstallationDisposition:
    """What an `installation`/`installation_repositories` delivery asks for —
    pure data so the decision is unit-testable without the DB."""

    installation_id: int
    connect: list[dict] = field(default_factory=list)  # raw GitHub repo dicts
    disconnect: list[dict] = field(default_factory=list)
    disconnect_all: bool = False  # App uninstalled → clear every matching repo


def installation_disposition(*, event: str, action: str, payload: dict) -> InstallationDisposition | None:
    """Pure dispatch for installation events. None → nothing to do (e.g.
    suspend/unsuspend/new_permissions_accepted)."""
    raw_id = (payload.get("installation") or {}).get("id")
    if not raw_id:
        return None
    installation_id = int(raw_id)

    if event == "installation":
        if action == "created":
            return InstallationDisposition(
                installation_id, connect=list(payload.get("repositories") or [])
            )
        if action == "deleted":
            return InstallationDisposition(installation_id, disconnect_all=True)
        return None

    if event == "installation_repositories":
        return InstallationDisposition(
            installation_id,
            connect=list(payload.get("repositories_added") or []),
            disconnect=list(payload.get("repositories_removed") or []),
        )
    return None


def _split_full_name(gh_repo: dict) -> tuple[str, str]:
    """(owner, name) from an installation-payload repo dict (`full_name`)."""
    full = str(gh_repo.get("full_name") or "")
    if "/" in full:
        owner, name = full.split("/", 1)
    else:
        owner, name = "", ""
    return owner, name or str(gh_repo.get("name") or "")


async def _find_registered_repos(gh_repo: dict) -> list[Repository]:
    owner, name = _split_full_name(gh_repo)
    return await _registered_repos(repo_id=gh_repo.get("id"), owner=owner, name=name)


async def _installation_org_uid(installation_id: int) -> str:
    """The org an installation is linked to ('' when not yet linked). Repos
    registered by multiple orgs share (owner, name, repo id) — installation
    LINK events must only touch the installation-org's nodes, never another
    tenant's."""
    link = await GitConnection.nodes.get_or_none(
        provider="github", external_id=str(installation_id)
    )
    return str(getattr(link, "org_uid", "") or "") if link is not None else ""


async def _handle_installation_event(*, event: str, action: str, payload: dict) -> dict:
    """Link/unlink installations onto ALREADY-registered repositories.

    Installation events never create Repository nodes — repos are registered
    by explicit selection (github_app.py: available-repos → register-repo).
    Newly available (not-yet-registered) repos are only counted in an
    `installation.repos_available` audit event; removing repos/uninstalling
    clears the installation id (the node itself is kept — history stays)."""
    disposition = installation_disposition(event=event, action=action, payload=payload)
    if disposition is None:
        return {"ok": True, "event": event, "action": action, "handled": False}

    now = datetime.now(UTC)
    available: list[str] = []
    linked: list[str] = []
    unlinked: list[str] = []

    if disposition.disconnect_all:
        nodes = await Repository.nodes.filter(github_installation_id=disposition.installation_id)
        for node in nodes:
            node.github_installation_id = None
            node.github_connection_status = "disconnected"
            node.updated_at = now
            await node.save()
            unlinked.append(node.slug)

    install_org = ""
    if disposition.connect:
        install_org = await _installation_org_uid(disposition.installation_id)

    for gh_repo in disposition.connect:
        owner, name = _split_full_name(gh_repo)
        if not name:
            continue
        nodes = await _find_registered_repos(gh_repo)
        # Cross-tenant guard: the same GitHub repo may be registered by several
        # orgs — link only the installation-org's node(s). An installation not
        # yet linked to an org keeps the legacy behavior (link every match).
        if install_org:
            nodes = [n for n in nodes if n.org_uid == install_org]
        if not nodes:
            # Available through the installation but not registered in OpenSweep —
            # surfaced via /api/v1/github/app/available-repos, never created here.
            available.append(f"{owner}/{name}" if owner else name)
            continue
        for node in nodes:
            node.github_installation_id = disposition.installation_id
            if gh_repo.get("id") is not None:
                node.github_repo_id = int(gh_repo["id"])
            if owner and not node.github_owner:
                node.github_owner = owner
            node.github_connection_status = "connected"
            node.updated_at = now
            await node.save()
            linked.append(node.slug)
            await write_audit(
                kind="repository.installation_linked",
                subject_uid=node.uid,
                subject_type="Repository",
                actor_uid="github-app",
                payload={"installation_id": disposition.installation_id, "event": event},
            )

    if available:
        await write_audit(
            kind="installation.repos_available",
            subject_uid=str(disposition.installation_id),
            subject_type="GitHubInstallation",
            actor_uid="github-app",
            payload={
                "installation_id": disposition.installation_id,
                "count": len(available),
                "repos": available,
                "event": event,
                "action": action,
            },
        )

    for gh_repo in disposition.disconnect:
        # Only nodes actually linked to THIS installation unlink — another
        # org's node for the same repo (own installation or PAT) is untouched.
        nodes = [
            n
            for n in await _find_registered_repos(gh_repo)
            if n.github_installation_id == disposition.installation_id
        ]
        for node in nodes:
            node.github_installation_id = None
            node.github_connection_status = "disconnected"
            node.updated_at = now
            await node.save()
            unlinked.append(node.slug)
            await write_audit(
                kind="repository.installation_unlinked",
                subject_uid=node.uid,
                subject_type="Repository",
                actor_uid="github-app",
                payload={"installation_id": disposition.installation_id, "event": event},
            )

    logger.info(
        f"installation webhook {event}/{action}: "
        f"available={available} linked={linked} unlinked={unlinked}",
        extra={"tag": "github"},
    )
    return {
        "ok": True,
        "event": event,
        "action": action,
        "available": available,
        "linked": linked,
        "unlinked": unlinked,
    }


async def _process_delivery(*, event: str, action: str, payload: dict) -> dict:
    if event in {"installation", "installation_repositories"}:
        return await _handle_installation_event(event=event, action=action, payload=payload)

    # Fan-out: the same GitHub repo may be registered by several orgs — the
    # event is processed against EVERY matching Repository node, each sync
    # scoped to its own repository_uid (no cross-tenant writes).
    repos = await _repos_for_payload(payload)
    if not repos:
        logger.info(
            f"webhook {event}/{action}: repository not registered in OpenSweep — ignored",
            extra={"tag": "delivery"},
        )
        return {"ok": True, "event": event, "registered": False}

    service = PullRequestService()
    synced_by_repo: list[tuple[Repository, list[int]]] = []
    failed: list[str] = []
    for repo in repos:
        numbers = await _pr_numbers_to_sync(event, payload, repo)
        synced: list[int] = []
        for number in dict.fromkeys(numbers):
            try:
                await service.sync_from_github(repo.uid, number)
                synced.append(number)
            except Exception as exc:
                failed.append(f"{repo.slug}#{number}: {exc}")
                logger.warning(
                    f"webhook sync failed for {repo.slug}#{number}: {exc}",
                    extra={"tag": "delivery"},
                )
        synced_by_repo.append((repo, synced))

    # A failed PR sync means state may be permanently missed (e.g. a merged
    # PR never marked merged) — fail the delivery BEFORE any follow-through
    # side effects (auto-review dispatch is not idempotent) so GitHub
    # redelivers and the whole handler reruns (for every org's node).
    # Reprocessing the syncs is safe: sync is head-driven and idempotent.
    if failed:
        raise RuntimeError("pr sync failed: " + "; ".join(failed))

    for repo, synced in synced_by_repo:
        await _delivery_follow_through(
            event=event, action=action, payload=payload, repo=repo, synced=synced
        )
    return {
        "ok": True,
        "event": event,
        "action": action,
        "synced": [n for _, synced in synced_by_repo for n in synced],
    }


async def _delivery_follow_through(
    *, event: str, action: str, payload: dict, repo: Repository, synced: list[int]
) -> None:
    """Post-sync side effects for ONE Repository node (best-effort tickets +
    doc freshness, auto-review dispatch) — runs only after every node's sync
    succeeded."""
    # Ticket Gate-2 follow-through: a merged PR completes its linked ticket
    # (audit "ticket.done_via_merge"). Best-effort — ticket bookkeeping must
    # never fail the webhook, mirroring the freshness bump below.
    for number in synced:
        try:
            pr = await PullRequest.nodes.get_or_none(pr_key=pr_key(repo.uid, number))
            if pr is not None and pr.state == "merged" and pr.ticket_uid:
                from domains.tickets.services.ticket_service import TicketService

                await TicketService().mark_done_via_merge(
                    pr.ticket_uid, pull_request_uid=pr.uid
                )
            if pr is not None and pr.state == "merged":
                from domains.threads.services.hooks import note_pr_merged

                await note_pr_merged(pr.uid)
        except Exception as exc:
            logger.warning(
                f"ticket done-via-merge failed for {repo.slug}#{number}: {exc}",
                extra={"tag": "delivery"},
            )

    # Webhook-driven doc freshness (§9): push events carry the changed paths,
    # so mark the Doc pages watching them stale (memory staleness + Checked
    # drift are computed against code_changed_at at read time) and auto-run any
    # on-event Investigations whose compute_dial allows it. The write-run
    # finalize does the same off its own changed paths, so this shared helper
    # is idempotent across both. Best-effort — never fails the webhook.
    if event == "push":
        changed_paths: list[str] = []
        for commit in payload.get("commits") or []:
            for key in ("added", "modified", "removed"):
                changed_paths.extend(str(p) for p in (commit.get(key) or []))
        from domains.agents.services.event_triggers import refresh_docs_for_change

        await refresh_docs_for_change(
            repository_uid=repo.uid, changed_paths=changed_paths, source="webhook"
        )

    # Auto-review (per-repo workflow config, stage `review`): dispatch a
    # review run when a PR opens or its head moves. Failures are logged,
    # never surfaced to GitHub — the PR simply stays not-converged (no
    # verdict) until a review lands.
    if event == "pull_request" and action in {
        "opened",
        "synchronize",
        "ready_for_review",
        "reopened",
    }:
        from domains.delivery.models import PullRequest as PRNode
        from domains.delivery.services.review_run_service import trigger_review_run
        from domains.runs.schemas import RunTrigger
        from domains.repositories.services.workflow import stage_auto

        if await stage_auto(repo.uid, "review"):
            for number in synced:
                pr = await PRNode.nodes.get_or_none(pr_key=pr_key(repo.uid, number))
                if pr is None or pr.state != "open" or pr.draft or not pr.head_sha:
                    continue
                try:
                    run = await trigger_review_run(pr, triggered_by="webhook", trigger=RunTrigger.EVENT)
                    logger.info(
                        f"auto review run {run.uid} dispatched for {repo.slug}#{number}",
                        extra={"tag": "delivery"},
                    )
                except Exception as exc:
                    logger.warning(
                        f"auto review dispatch failed for {repo.slug}#{number}: {exc}",
                        extra={"tag": "delivery"},
                    )
