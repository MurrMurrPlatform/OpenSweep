"""Public OAuth 2.1 gateway endpoints for `opensweep connect` (MCP auth).

MCP clients discover these per the MCP authorization spec:
  1. 401 from /mcp carries `WWW-Authenticate: … resource_metadata="…"`.
  2. GET /.well-known/oauth-protected-resource → points at this gateway as
     the authorization server.
  3. GET /.well-known/oauth-authorization-server → endpoints + capabilities.
  4. POST /oauth/register (RFC 7591 dynamic client registration, public
     client, PKCE-only).
  5. GET /oauth/authorize → redirects the browser into the SPA's consent
     view (the user authenticates there via the existing Zitadel OIDC
     login; the SPA calls the authenticated approve endpoint, which mints
     the code).
  6. POST /oauth/token → code exchange (PKCE-verified) and refresh-token
     rotation. Tokens are opaque `osmcp_`/`osmcr_` values, hashed at rest.

Every endpoint here is intentionally auth-exempt (TokenAuthMiddleware) —
they are the front door for clients that do not yet hold credentials.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from config import settings
from domains.oauth_mcp.services import oauth_service

router = APIRouter(tags=["oauth-mcp"])


def _backend_base(request: Request) -> str:
    configured = (settings.OPENSWEEP_WEBHOOK_PUBLIC_BASE_URL or "").rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _frontend_base(request: Request) -> str:
    configured = (settings.OPENSWEEP_FRONTEND_BASE_URL or "").rstrip("/")
    return configured or _backend_base(request)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(request: Request):
    base = _backend_base(request)
    return {
        "resource": f"{base}{settings.MCP_MOUNT_PATH or '/mcp'}",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp:read", "mcp:write"],
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
async def authorization_server_metadata(request: Request):
    base = _backend_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp:read", "mcp:write"],
    }


class RegisterRequest(BaseModel):
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_name: str = ""
    # Accepted-and-ignored RFC 7591 fields clients commonly send.
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str | None = None
    scope: str | None = None


@router.post("/oauth/register", status_code=201)
async def register(req: RegisterRequest):
    client = await oauth_service.register_client(
        name=req.client_name, redirect_uris=req.redirect_uris
    )
    return {
        "client_id": client.uid,
        "client_name": client.name,
        "redirect_uris": client.redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _error_redirect(redirect_uri: str, state: str, error: str) -> RedirectResponse:
    params = {"error": error}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
):
    """Validate the request, then hand the browser to the SPA consent view.
    The SPA authenticates the user (existing Zitadel login) and calls the
    authenticated approve endpoint, which mints the code and returns the
    final redirect."""
    client = await oauth_service.get_client(client_id)
    if not oauth_service.redirect_uri_allowed(redirect_uri, client.redirect_uris or []):
        # Never redirect to an unregistered URI (open-redirect hardening).
        return JSONResponse(status_code=400, content={"error": "invalid_redirect_uri"})
    if response_type != "code":
        return _error_redirect(redirect_uri, state, "unsupported_response_type")
    if code_challenge_method != "S256" or not code_challenge:
        # OAuth 2.1: PKCE S256 is mandatory for public clients.
        return _error_redirect(redirect_uri, state, "invalid_request")
    try:
        normalized_scope = oauth_service.normalize_scope(scope)
    except Exception:  # noqa: BLE001 — unsupported scope
        return _error_redirect(redirect_uri, state, "invalid_scope")

    # Deliberately NOT forwarding client.name — the consent view fetches
    # /api/v1/oauth-mcp/client_metadata as the logged-in user for the
    # server-truth name, so a hand-crafted /connect/authorize URL cannot
    # substitute a friendly name over the top of a different client_id.
    consent = urlencode(
        {
            "client_id": client.uid,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "scope": normalized_scope,
        }
    )
    return RedirectResponse(f"{_frontend_base(request)}/connect/authorize?{consent}", status_code=302)


@router.post("/oauth/token")
async def token(
    grant_type: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
):
    if grant_type == "authorization_code":
        pair = await oauth_service.exchange_code(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
    elif grant_type == "refresh_token":
        pair = await oauth_service.refresh_tokens(
            refresh_token=refresh_token, client_id=client_id
        )
    else:
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    return {
        "access_token": pair._access_token,  # noqa: SLF001 — transient cleartext
        "refresh_token": pair._refresh_token,  # noqa: SLF001
        "token_type": "Bearer",
        "expires_in": int(oauth_service.ACCESS_TTL.total_seconds()),
        "scope": pair.scope,
    }
