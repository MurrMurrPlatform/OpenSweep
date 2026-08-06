"""Slack connection + notification-rule management (org-scoped).

Connection semantics mirror GitConnection: a Slack workspace (team_id) links
to exactly ONE org, first-wins — relinking would silently hand one tenant's
Slack workspace to another. An org holds at most one connection; reinstalling
the same workspace rotates the token in place, installing a different
workspace replaces the connection AND drops the org's rules (channel ids are
meaningless outside their workspace).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from config import settings
from domains.slack import client as slack_client
from domains.slack.events import EVENT_TYPES
from domains.slack.models import SlackConnection, SlackNotificationRule
from domains.slack.schemas import (
    CreateSlackRuleRequest,
    SlackChannelDTO,
    SlackRuleDTO,
    UpdateSlackRuleRequest,
)
from infrastructure import secretbox
from infrastructure.audit import write_audit
from logging_config import logger

# Bot scopes requested at install time:
#   chat:write / chat:write.public — post notifications + replies (public
#     channels without an explicit invite)
#   channels:read / groups:read    — channel picker in the rules editor
#   app_mentions:read              — "@OpenSweep …" in channels
#   im:history / im:write          — DM conversations with the bot
#   commands                       — the /opensweep slash command
BOT_SCOPES = (
    "chat:write",
    "chat:write.public",
    "channels:read",
    "groups:read",
    "app_mentions:read",
    "im:history",
    "im:write",
    "commands",
)


def slack_app_configured() -> bool:
    """All three credentials, not just the OAuth pair — without the signing
    secret the inbound surface (events, commands) rejects everything, which
    presents as "connected but the bot ignores everyone"."""
    return bool(
        settings.SLACK_CLIENT_ID
        and settings.SLACK_CLIENT_SECRET
        and settings.SLACK_SIGNING_SECRET
    )


def oauth_redirect_uri() -> str:
    base = settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/api/v1/slack/oauth/callback"


def build_authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.SLACK_CLIENT_ID,
            "scope": ",".join(BOT_SCOPES),
            "state": state,
            "redirect_uri": oauth_redirect_uri(),
        }
    )
    return f"https://slack.com/oauth/v2/authorize?{query}"


async def get_connection(org_uid: str) -> SlackConnection | None:
    rows = await SlackConnection.nodes.filter(org_uid=org_uid)
    return rows[0] if rows else None


def bot_token(connection: SlackConnection) -> str:
    """Unseal the bot token — raises SecretBoxError when undecryptable."""
    return secretbox.unseal(connection.bot_token_sealed)


async def store_connection(
    org_uid: str, oauth_payload: dict[str, Any], *, installed_by: str
) -> SlackConnection:
    """Persist an oauth.v2.access response for an org.

    - same workspace reinstalled → rotate token/name/scopes in place
    - different workspace → replace the connection and drop the org's rules
    - workspace already linked to ANOTHER org → 409, never re-pointed
    """
    team = oauth_payload.get("team") or {}
    team_id = str(team.get("id") or "").strip()
    access_token = str(oauth_payload.get("access_token") or "").strip()
    if not team_id or not access_token:
        raise HTTPException(status_code=502, detail="Slack OAuth response missing team or token")

    claimed = await SlackConnection.nodes.get_or_none(team_id=team_id)
    if claimed is not None and claimed.org_uid != org_uid:
        raise HTTPException(
            status_code=409,
            detail="This Slack workspace is already connected to another organization",
        )

    existing = await get_connection(org_uid)
    if existing is not None and existing.team_id != team_id:
        # Workspace switch: old channel ids can't be trusted in the new team.
        await delete_org_rules(org_uid)
        await existing.delete()
        existing = None

    sealed = secretbox.seal(access_token)
    scopes = str(oauth_payload.get("scope") or "")
    bot_user_id = str(oauth_payload.get("bot_user_id") or "")
    team_name = str(team.get("name") or "")

    if existing is not None:
        existing.bot_token_sealed = sealed
        existing.team_name = team_name
        existing.bot_user_id = bot_user_id
        existing.scopes = scopes
        existing.installed_by = installed_by
        connection = await existing.save()
    else:
        connection = await SlackConnection(
            org_uid=org_uid,
            team_id=team_id,
            team_name=team_name,
            bot_user_id=bot_user_id,
            bot_token_sealed=sealed,
            scopes=scopes,
            installed_by=installed_by,
        ).save()

    await write_audit(
        kind="slack.connected",
        subject_uid=connection.uid,
        subject_type="SlackConnection",
        actor_uid=installed_by,
        payload={"team_id": team_id, "team_name": team_name},
        repository_uid="",
    )
    return connection


async def delete_connection(org_uid: str, *, actor_uid: str) -> bool:
    """Revoke the token (best-effort) and remove the connection. Rules are
    KEPT — they're inert without a connection and resume when the same
    workspace reconnects (a workspace *switch* drops them in
    store_connection, where the old channel ids become meaningless)."""
    connection = await get_connection(org_uid)
    if connection is None:
        return False
    try:
        await slack_client.revoke_token(bot_token(connection))
    except Exception as exc:  # noqa: BLE001 — a sealed-token failure must not block disconnect
        logger.warning(f"Slack token revoke skipped: {exc}", extra={"tag": "slack"})
    team_id = connection.team_id
    await connection.delete()
    await write_audit(
        kind="slack.disconnected",
        subject_uid=connection.uid,
        subject_type="SlackConnection",
        actor_uid=actor_uid,
        payload={"team_id": team_id},
        repository_uid="",
    )
    return True


async def connection_for_team(team_id: str) -> SlackConnection | None:
    return await SlackConnection.nodes.get_or_none(team_id=team_id)


async def list_channels(connection: SlackConnection) -> list[SlackChannelDTO]:
    channels = await slack_client.list_channels(bot_token(connection))
    return sorted(
        (
            SlackChannelDTO(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                is_private=bool(c.get("is_private")),
            )
            for c in channels
            if c.get("id")
        ),
        key=lambda c: c.name,
    )


# ── Notification rules ───────────────────────────────────────────────────────


def rule_to_dto(rule: SlackNotificationRule) -> SlackRuleDTO:
    return SlackRuleDTO(
        uid=rule.uid,
        event_type=rule.event_type,
        channel_id=rule.channel_id,
        channel_name=rule.channel_name or "",
        repository_uid=rule.repository_uid or "",
        enabled=bool(rule.enabled),
        created_by=rule.created_by or "",
    )


async def list_rules(org_uid: str) -> list[SlackNotificationRule]:
    rules = await SlackNotificationRule.nodes.filter(org_uid=org_uid)
    return sorted(rules, key=lambda r: (r.event_type, r.channel_name or r.channel_id))


async def get_rule(org_uid: str, rule_uid: str) -> SlackNotificationRule:
    rule = await SlackNotificationRule.nodes.get_or_none(uid=rule_uid)
    if rule is None or rule.org_uid != org_uid:
        # Tenancy: cross-org existence never leaks.
        raise HTTPException(status_code=404, detail="rule not found")
    return rule


async def _validate_rule_repo(org_uid: str, repository_uid: str) -> None:
    if not repository_uid:
        return
    from domains.tenancy import require_repo_in_org

    await require_repo_in_org(repository_uid, org_uid)


async def create_rule(
    org_uid: str, req: CreateSlackRuleRequest, *, created_by: str
) -> SlackNotificationRule:
    if req.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=422, detail=f"unknown event_type {req.event_type!r}")
    if await get_connection(org_uid) is None:
        raise HTTPException(status_code=409, detail="Connect Slack before adding rules")
    await _validate_rule_repo(org_uid, req.repository_uid)
    duplicates = await SlackNotificationRule.nodes.filter(
        org_uid=org_uid, event_type=req.event_type, channel_id=req.channel_id
    )
    if any((d.repository_uid or "") == req.repository_uid for d in duplicates):
        raise HTTPException(status_code=409, detail="An identical rule already exists")
    rule = await SlackNotificationRule(
        org_uid=org_uid,
        event_type=req.event_type,
        channel_id=req.channel_id,
        channel_name=req.channel_name,
        repository_uid=req.repository_uid,
        enabled=req.enabled,
        created_by=created_by,
    ).save()
    await write_audit(
        kind="slack.rule_created",
        subject_uid=rule.uid,
        subject_type="SlackNotificationRule",
        actor_uid=created_by,
        payload={"event_type": rule.event_type, "channel_id": rule.channel_id},
        repository_uid="",
    )
    return rule


async def update_rule(
    org_uid: str, rule_uid: str, req: UpdateSlackRuleRequest, *, actor_uid: str
) -> SlackNotificationRule:
    rule = await get_rule(org_uid, rule_uid)
    if req.event_type is not None:
        if req.event_type not in EVENT_TYPES:
            raise HTTPException(status_code=422, detail=f"unknown event_type {req.event_type!r}")
        rule.event_type = req.event_type
    if req.channel_id is not None:
        if not req.channel_id.strip():
            raise HTTPException(status_code=422, detail="channel_id cannot be empty")
        rule.channel_id = req.channel_id.strip()
    if req.channel_name is not None:
        rule.channel_name = req.channel_name
    if req.repository_uid is not None:
        await _validate_rule_repo(org_uid, req.repository_uid)
        rule.repository_uid = req.repository_uid
    if req.enabled is not None:
        rule.enabled = req.enabled
    # Mirror create_rule's dedupe: no other rule in this org may end up with
    # the same (event_type, channel_id, repository_uid) tuple this update
    # would produce. Without this an edit can quietly land on another rule's
    # identity and duplicate delivery.
    duplicates = await SlackNotificationRule.nodes.filter(
        org_uid=org_uid, event_type=rule.event_type, channel_id=rule.channel_id
    )
    if any(
        d.uid != rule.uid and (d.repository_uid or "") == (rule.repository_uid or "")
        for d in duplicates
    ):
        raise HTTPException(status_code=409, detail="An identical rule already exists")
    rule = await rule.save()
    await write_audit(
        kind="slack.rule_updated",
        subject_uid=rule.uid,
        subject_type="SlackNotificationRule",
        actor_uid=actor_uid,
        payload={"event_type": rule.event_type, "channel_id": rule.channel_id},
        repository_uid="",
    )
    return rule


async def delete_rule(org_uid: str, rule_uid: str, *, actor_uid: str) -> None:
    rule = await get_rule(org_uid, rule_uid)
    await rule.delete()
    await write_audit(
        kind="slack.rule_deleted",
        subject_uid=rule_uid,
        subject_type="SlackNotificationRule",
        actor_uid=actor_uid,
        payload={},
        repository_uid="",
    )


async def delete_org_rules(org_uid: str) -> int:
    rules = await SlackNotificationRule.nodes.filter(org_uid=org_uid)
    for rule in rules:
        await rule.delete()
    return len(rules)
