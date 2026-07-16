"""TokenAuthMiddleware — the shared-token internet-hardening layer (§13).

Matrix tests run against a tiny purpose-built app (no DB); the exemption
tests run against the real app object so the wiring (middleware order,
webhook + health carve-outs, gated /docs) is what's actually asserted.
"""

import hashlib
import hmac

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import TokenAuthMiddleware
from app import app as real_app
from config import settings

TOKEN = "sekrit-token"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A minimal app wrapped in TokenAuthMiddleware, auth enabled."""
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    mini = FastAPI()

    @mini.get("/health")
    async def health():
        return {"ok": True}

    @mini.get("/api/v1/thing")
    async def thing():
        return {"ok": True}

    @mini.post("/api/v1/github/webhook")
    async def webhook():
        return {"ok": True}

    @mini.websocket("/api/v1/sessions/x/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    mini.add_middleware(TokenAuthMiddleware)
    return TestClient(mini)


# ── HTTP matrix ──────────────────────────────────────────────────────────────


def test_denies_without_token(client):
    res = client.get("/api/v1/thing")
    assert res.status_code == 401
    assert res.json() == {"detail": "unauthorized"}
    assert res.headers["www-authenticate"] == "Bearer"


def test_denies_wrong_token(client):
    assert client.get("/api/v1/thing", headers={"X-OpenSweep-Auth": "nope"}).status_code == 401
    assert (
        client.get("/api/v1/thing", headers={"Authorization": "Bearer nope"}).status_code == 401
    )


def test_allows_bearer_header(client):
    res = client.get("/api/v1/thing", headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200


def test_allows_x_opensweep_auth_header(client):
    res = client.get("/api/v1/thing", headers={"X-OpenSweep-Auth": TOKEN})
    assert res.status_code == 200


def test_legacy_x_koala_auth_header_no_longer_honored(client):
    # The Koala-era rebrand-compat header has been removed (F8): even the
    # correct token in X-Koala-Auth must NOT authenticate — only the
    # X-OpenSweep-Auth / Authorization surfaces remain.
    assert client.get("/api/v1/thing", headers={"X-Koala-Auth": TOKEN}).status_code == 401


def test_query_param_rejected_on_plain_rest(client):
    # auth_token in the URL is only honored for WebSocket handshakes.
    assert client.get(f"/api/v1/thing?auth_token={TOKEN}").status_code == 401


def test_health_exempt(client):
    assert client.get("/health").status_code == 200


def test_webhook_exempt(client):
    assert client.post("/api/v1/github/webhook").status_code == 200


# ── WebSocket matrix ─────────────────────────────────────────────────────────


def test_ws_denied_without_token_closes_4401(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/v1/sessions/x/ws") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4401


def test_ws_allows_query_param(client):
    with client.websocket_connect(f"/api/v1/sessions/x/ws?auth_token={TOKEN}") as ws:
        assert ws.receive_json() == {"ok": True}


def test_ws_allows_header(client):
    with client.websocket_connect(
        "/api/v1/sessions/x/ws", headers={"X-OpenSweep-Auth": TOKEN}
    ) as ws:
        assert ws.receive_json() == {"ok": True}


# ── Auth disabled (default) ──────────────────────────────────────────────────


def test_disabled_when_token_unset(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", "")
    mini = FastAPI()

    @mini.get("/api/v1/thing")
    async def thing():
        return {"ok": True}

    mini.add_middleware(TokenAuthMiddleware)
    assert TestClient(mini).get("/api/v1/thing").status_code == 200


# ── Real-app wiring ──────────────────────────────────────────────────────────


def test_real_app_gates_openapi_and_docs(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    client = TestClient(real_app)
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json", headers={"X-OpenSweep-Auth": TOKEN}).status_code == 200


def test_real_app_health_stays_open(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    assert TestClient(real_app).get("/health").status_code == 200


def test_real_app_webhook_reaches_hmac_verification(monkeypatch):
    """No shared token needed — the webhook has its own HMAC trust model.
    A correctly signed ping must reach the receiver and succeed."""
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "hooksecret")
    body = b"{}"
    signature = "sha256=" + hmac.new(b"hooksecret", body, hashlib.sha256).hexdigest()
    res = TestClient(real_app).post(
        "/api/v1/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "ping",
            "Content-Type": "application/json",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "event": "ping"}


def test_real_app_webhook_bad_signature_is_401_from_hmac_not_middleware(monkeypatch):
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "hooksecret")
    res = TestClient(real_app).post(
        "/api/v1/github/webhook",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "ping"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid webhook signature"


def test_mcp_bridge_carries_a_scoped_run_token_not_the_platform_token(monkeypatch, tmp_path):
    """In-cluster executors call back through /mcp/platform — when auth is
    on, the generated SSE server config presents a per-run `osrt_…` token,
    NEVER the platform-wide OPENSWEEP_AUTH_TOKEN (the agent can read this file)."""
    import json

    from domains.executors.mcp_bridge import write_claude_mcp_config
    from infrastructure.run_tokens import mint_run_token

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET", raising=False)
    path = write_claude_mcp_config(run_uid="run123", scratch_root=str(tmp_path))
    server = json.loads(open(path).read())["mcpServers"]["opensweep-platform"]
    assert server["type"] == "sse"
    assert server["url"].endswith(settings.MCP_PLATFORM_TOOL_MOUNT_PATH)
    assert server["headers"]["X-OpenSweep-Auth"] == mint_run_token("run123")
    assert server["headers"]["X-OpenSweep-Run-Uid"] == "run123"
    assert TOKEN not in open(path).read(), "platform token leaked into agent mcp.json"

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", "")
    path = write_claude_mcp_config(run_uid="run456", scratch_root=str(tmp_path))
    server = json.loads(open(path).read())["mcpServers"]["opensweep-platform"]
    assert "X-OpenSweep-Auth" not in server["headers"]


def test_mcp_bridge_mints_run_token_from_dedicated_secret(monkeypatch, tmp_path):
    """Zitadel-only deployments (OPENSWEEP_AUTH_TOKEN empty) authenticate executor
    callbacks via OPENSWEEP_RUN_TOKEN_SECRET — the bridge must attach the osrt_
    header whenever a token can be minted, not only when the shared token is
    set (regression: server connected but every tool call 401'd)."""
    import json

    from domains.executors.mcp_bridge import write_claude_mcp_config
    from infrastructure.run_tokens import mint_run_token

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", "")
    monkeypatch.setenv("OPENSWEEP_RUN_TOKEN_SECRET", "dedicated-secret")
    path = write_claude_mcp_config(run_uid="run789", scratch_root=str(tmp_path))
    server = json.loads(open(path).read())["mcpServers"]["opensweep-platform"]
    assert server["headers"]["X-OpenSweep-Auth"] == mint_run_token("run789")
    assert server["headers"]["X-OpenSweep-Auth"].startswith("osrt_")


def test_run_token_config_error_flags_zitadel_only_gap(monkeypatch):
    """Auth enforced via ZITADEL_ISSUER alone + no run-token secret = every
    executor callback 401s. The helper names the gap; dispatch fails fast on it."""
    from infrastructure.run_tokens import run_token_config_error

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "ZITADEL_ISSUER", "https://idp.example.com")
    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET", raising=False)
    assert "OPENSWEEP_RUN_TOKEN_SECRET" in run_token_config_error()

    monkeypatch.setenv("OPENSWEEP_RUN_TOKEN_SECRET", "s3cret")
    assert run_token_config_error() == ""

    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(settings, "ZITADEL_ISSUER", "")
    assert run_token_config_error() == ""


# ── Per-run scoped tokens (infrastructure/run_tokens.py) ─────────────────────


def test_run_token_mint_and_verify(monkeypatch):
    from infrastructure import run_tokens

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET", raising=False)
    token = run_tokens.mint_run_token("run123")
    assert token.startswith("osrt_") and len(token) == len("osrt_") + 40
    assert run_tokens.verify_run_token(token, "run123")
    assert not run_tokens.verify_run_token(token, "run456")  # wrong uid
    assert not run_tokens.verify_run_token("osrt_" + "0" * 40, "run123")  # wrong token
    assert not run_tokens.verify_run_token("", "run123")
    assert not run_tokens.verify_run_token(token, "")
    # Auth disabled → nothing minted, nothing verifiable.
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", "")
    assert run_tokens.mint_run_token("run123") == ""
    assert not run_tokens.verify_run_token(token, "run123")


def test_run_token_dedicated_secret_takes_precedence(monkeypatch):
    from infrastructure import run_tokens

    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OPENSWEEP_RUN_TOKEN_SECRET", "dedicated")
    with_dedicated = run_tokens.mint_run_token("run123")
    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET")
    with_shared = run_tokens.mint_run_token("run123")
    assert with_dedicated != with_shared


@pytest.fixture
def run_token_client(monkeypatch) -> TestClient:
    """Mini app with a platform-tools route and a plain route, auth enabled."""
    monkeypatch.setattr(settings, "OPENSWEEP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("OPENSWEEP_RUN_TOKEN_SECRET", raising=False)
    mini = FastAPI()

    @mini.post("/api/v1/platform-tools/opensweep_report_finding")
    async def tool():
        return {"ok": True}

    @mini.get("/api/v1/platform-read/docs")
    async def read_tool():
        return {"ok": True}

    @mini.get("/mcp/platform")
    async def mcp():
        return {"ok": True}

    @mini.get("/api/v1/thing")
    async def thing():
        return {"ok": True}

    mini.add_middleware(TokenAuthMiddleware)
    return TestClient(mini)


def test_run_token_accepted_on_platform_tools_with_matching_uid(monkeypatch, run_token_client):
    from infrastructure.run_tokens import mint_run_token

    token = mint_run_token("run123")
    res = run_token_client.post(
        "/api/v1/platform-tools/opensweep_report_finding",
        headers={"X-OpenSweep-Auth": token, "X-OpenSweep-Run-Uid": "run123"},
    )
    assert res.status_code == 200
    res = run_token_client.get(
        "/mcp/platform", headers={"X-OpenSweep-Auth": token, "X-OpenSweep-Run-Uid": "run123"}
    )
    assert res.status_code == 200
    # The look-before-write read tools re-enter at /api/v1/platform-read —
    # the token must be valid there too (regression: MCP tool calls 401'd).
    res = run_token_client.get(
        "/api/v1/platform-read/docs",
        headers={"X-OpenSweep-Auth": token, "X-OpenSweep-Run-Uid": "run123"},
    )
    assert res.status_code == 200


def test_run_token_rejected_outside_platform_tool_paths(run_token_client):
    from infrastructure.run_tokens import mint_run_token

    token = mint_run_token("run123")
    res = run_token_client.get(
        "/api/v1/thing", headers={"X-OpenSweep-Auth": token, "X-OpenSweep-Run-Uid": "run123"}
    )
    assert res.status_code == 401


def test_run_token_rejected_with_wrong_or_missing_uid(run_token_client):
    from infrastructure.run_tokens import mint_run_token

    token = mint_run_token("run123")
    # Wrong uid: token was minted for another run.
    res = run_token_client.post(
        "/api/v1/platform-tools/opensweep_report_finding",
        headers={"X-OpenSweep-Auth": token, "X-OpenSweep-Run-Uid": "run999"},
    )
    assert res.status_code == 401
    # Missing uid header: both headers are required.
    res = run_token_client.post(
        "/api/v1/platform-tools/opensweep_report_finding", headers={"X-OpenSweep-Auth": token}
    )
    assert res.status_code == 401


def test_run_token_rejected_when_forged(run_token_client):
    res = run_token_client.post(
        "/api/v1/platform-tools/opensweep_report_finding",
        headers={"X-OpenSweep-Auth": "osrt_" + "f" * 40, "X-OpenSweep-Run-Uid": "run123"},
    )
    assert res.status_code == 401


def test_platform_token_still_works_on_platform_tool_paths(run_token_client):
    res = run_token_client.post(
        "/api/v1/platform-tools/opensweep_report_finding", headers={"X-OpenSweep-Auth": TOKEN}
    )
    assert res.status_code == 200
