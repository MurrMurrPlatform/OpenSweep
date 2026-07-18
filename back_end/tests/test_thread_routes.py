"""Thread route surface is mounted with the expected paths + methods."""

from app import app


def test_thread_routes_are_mounted():
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/threads" in paths
    assert "/api/v1/threads/{uid}" in paths
    assert "/api/v1/threads/{uid}/plan" in paths
    assert "/api/v1/threads/{uid}/plan/approve" in paths
    assert "/api/v1/threads/{uid}/implement" in paths
    assert "/api/v1/threads/{uid}/abandon" in paths
