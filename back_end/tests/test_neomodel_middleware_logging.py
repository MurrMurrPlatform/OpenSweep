"""A failed Neomodel async-driver setup must be logged loudly.

The middleware swallows the exception on purpose (the request continues and
fails on its own terms), but it used to log at DEBUG — so in production a
Neo4j outage produced unexplained request failures with nothing in the logs
pointing at the database.
"""

import pytest
from neomodel import adb
from neomodel import config as neomodel_conf

from app import NeomodelAsyncDriverMiddleware


@pytest.mark.asyncio
async def test_driver_setup_failure_is_logged_at_error(monkeypatch, caplog):
    called = []

    async def _downstream(scope, receive, send):
        called.append(scope["type"])

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("bolt handshake refused")

    monkeypatch.setattr(neomodel_conf, "DATABASE_URL", "bolt://neo4j:pw@nowhere:7687")
    monkeypatch.setattr(adb, "driver", None, raising=False)
    monkeypatch.setattr(adb, "set_connection", _boom, raising=False)

    middleware = NeomodelAsyncDriverMiddleware(_downstream)
    with caplog.at_level("DEBUG"):
        await middleware({"type": "http"}, None, None)

    # The request still reaches the app — the middleware must not break it.
    assert called == ["http"]

    records = [r for r in caplog.records if "bolt handshake refused" in r.getMessage()]
    assert records, "driver setup failure was not logged at all"
    assert all(r.levelname == "ERROR" for r in records)
    assert any(r.exc_info for r in records), "traceback was not attached"
