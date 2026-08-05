"""Authenticated consent endpoint for the `opensweep connect` OAuth gateway.

The SPA consent view (/connect/authorize) calls this as the logged-in user
(Zitadel OIDC bearer). Approval re-validates everything against the DB —
the browser-carried params are untrusted — checks the org's connect
entitlement, mints the single-use authorization code, and returns the final
client redirect for the SPA to follow.

The `/client_metadata` endpoint lets the consent view render the
server-truth client name + registered redirect URIs, so a hand-crafted
`/connect/authorize?client_name=…` URL cannot show the user a spoofed
identity for the client whose code will be minted.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.dependencies import get_current_user
from domains.oauth_mcp.services import oauth_service
from domains.oauth_mcp.services.entitlements import can_use_connect
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/oauth-mcp", tags=["oauth-mcp"])


@router.get("/client_metadata", operation_id="opensweep_oauth_mcp_client_metadata")
async def client_metadata(
    client_id: str,
    redirect_uri: str = "",
    _user: UserDTO = Depends(get_current_user),
) -> dict:
    """Return the DB-truth client name + registered redirect URIs so the
    consent view can display them without trusting URL query params.

    Authenticated (any logged-in user): reveals only the client's public
    registration data — the same fields the client itself sees back from
    /oauth/register. `redirect_uri` is optional; when present the response's
    `redirect_uri_registered` flag tells the view whether to show it as a
    trusted destination or flag the link as malformed."""
    client = await oauth_service.get_client(client_id)
    registered = client.redirect_uris or []
    return {
        "client_id": client.uid,
        "client_name": client.name or "",
        "redirect_uris": registered,
        "redirect_uri_registered": (
            bool(redirect_uri)
            and oauth_service.redirect_uri_allowed(redirect_uri, registered)
        ),
    }


class ApproveRequest(BaseModel):
    client_id: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)
    state: str = ""
    code_challenge: str = Field(min_length=1)
    scope: str = "mcp:read"


@router.post("/approve", operation_id="opensweep_oauth_mcp_approve")
async def approve(req: ApproveRequest, user: UserDTO = Depends(get_current_user)) -> dict:
    if not await can_use_connect(user.org_uid):
        raise HTTPException(
            status_code=403, detail="connecting local agents is not enabled for this organization"
        )
    client = await oauth_service.get_client(req.client_id)
    if not oauth_service.redirect_uri_allowed(req.redirect_uri, client.redirect_uris or []):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    scope = oauth_service.normalize_scope(req.scope)
    code = await oauth_service.issue_code(
        client=client,
        user_uid=user.uid,
        scope=scope,
        code_challenge=req.code_challenge,
        redirect_uri=req.redirect_uri,
    )
    await write_audit(
        kind="oauth_mcp.consent_granted",
        subject_uid=client.uid,
        subject_type="OAuthClient",
        actor_uid=user.uid,
        payload={"scope": scope, "client_name": client.name},
    )
    from urllib.parse import urlencode

    params = {"code": code}
    if req.state:
        params["state"] = req.state
    sep = "&" if "?" in req.redirect_uri else "?"
    return {"redirect_to": f"{req.redirect_uri}{sep}{urlencode(params)}"}


class DenyRequest(BaseModel):
    client_id: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)
    state: str = ""


@router.post("/deny", operation_id="opensweep_oauth_mcp_deny")
async def deny(req: DenyRequest, user: UserDTO = Depends(get_current_user)) -> dict:
    """Denial redirect, server-validated: the browser-carried redirect_uri is
    only followed when it is registered for the client — a raw client-side
    redirect would be an open redirect from our origin."""
    client = await oauth_service.get_client(req.client_id)
    if not oauth_service.redirect_uri_allowed(req.redirect_uri, client.redirect_uris or []):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    await write_audit(
        kind="oauth_mcp.consent_denied",
        subject_uid=client.uid,
        subject_type="OAuthClient",
        actor_uid=user.uid,
        payload={"client_name": client.name},
    )
    from urllib.parse import urlencode

    params = {"error": "access_denied"}
    if req.state:
        params["state"] = req.state
    sep = "&" if "?" in req.redirect_uri else "?"
    return {"redirect_to": f"{req.redirect_uri}{sep}{urlencode(params)}"}
