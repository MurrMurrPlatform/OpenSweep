"""Attention API surface — route mounted, tenancy 404, and the pure
assembly helper (priority ordering, counts rollup). DB-free.
"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import api.v1.attention as attention_mod
from app import app
from domains.metrics.schemas import AttentionItem, RepoAttention
from domains.metrics.services.attention_service import KIND_PRIORITY, assemble
from domains.users.schemas import UserDTO


def _openapi_paths() -> set[str]:
    return set(app.openapi().get("paths", {}).keys())


def _openapi_operation_ids() -> set[str]:
    ops = set()
    for methods in app.openapi().get("paths", {}).values():
        for op in methods.values():
            if isinstance(op, dict) and op.get("operationId"):
                ops.add(op["operationId"])
    return ops


def test_attention_route_is_mounted():
    assert "/api/v1/repositories/{repository_uid}/attention" in _openapi_paths()


def test_attention_operation_id():
    assert "opensweep_repository_attention" in _openapi_operation_ids()


def _user() -> UserDTO:
    return UserDTO(
        uid="u1",
        email="u@example.test",
        display_name="U",
        role="admin",
        org_uid="org-a",
        org_role="owner",
        is_platform_admin=False,
    )


async def test_cross_org_is_404(monkeypatch):
    async def deny(repository_uid, org_uid):
        raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(attention_mod, "require_repo_in_org", deny)
    with pytest.raises(HTTPException) as exc:
        await attention_mod.repository_attention("repo-x", user=_user())
    assert exc.value.status_code == 404


async def test_route_returns_service_result(monkeypatch):
    async def allow(repository_uid, org_uid):
        return None

    sentinel = RepoAttention(
        repository_uid="repo-a", generated_at=datetime.now(UTC), total=0
    )

    async def fake_attention(repository_uid):
        assert repository_uid == "repo-a"
        return sentinel

    monkeypatch.setattr(attention_mod, "require_repo_in_org", allow)
    monkeypatch.setattr(
        attention_mod.attention_service, "repo_attention", fake_attention
    )
    assert await attention_mod.repository_attention("repo-a", user=_user()) is sentinel


# ── Pure assembly helper ────────────────────────────────────────────────────


def _item(kind: str, *, count: int = 1, created_at: datetime | None = None) -> AttentionItem:
    return AttentionItem(
        kind=kind,
        priority=KIND_PRIORITY[kind],
        title=kind,
        subject_type="PullRequest",
        subject_uid="x",
        count=count,
        created_at=created_at,
    )


def test_kind_priorities_are_unique_and_kill_switch_first():
    assert len(set(KIND_PRIORITY.values())) == len(KIND_PRIORITY)
    assert KIND_PRIORITY["kill_switch_engaged"] == min(KIND_PRIORITY.values())


def test_assemble_orders_by_priority_then_newest():
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 6, 1, tzinfo=UTC)
    result = assemble(
        [
            _item("pr_ci_red", created_at=old),
            _item("kill_switch_engaged"),
            _item("pr_ci_red", created_at=new),
            _item("verdict_needs_human", created_at=old),
        ],
        "repo-a",
    )
    kinds = [i.kind for i in result.items]
    assert kinds == [
        "kill_switch_engaged",
        "verdict_needs_human",
        "pr_ci_red",
        "pr_ci_red",
    ]
    # Within a kind, newest first.
    assert result.items[2].created_at == new
    assert result.items[3].created_at == old


def test_assemble_counts_sum_item_counts():
    result = assemble(
        [
            _item("pr_waive_requested", count=3),
            _item("pr_waive_requested", count=2),
            _item("high_severity_findings", count=7),
        ],
        "repo-a",
    )
    assert result.counts_by_kind == {
        "pr_waive_requested": 5,
        "high_severity_findings": 7,
    }
    assert result.total == 12
    assert result.repository_uid == "repo-a"


def test_assemble_empty():
    result = assemble([], "repo-a")
    assert result.total == 0
    assert result.items == []
    assert result.counts_by_kind == {}
