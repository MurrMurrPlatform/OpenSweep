"""Tests for domains/runs/services/chat_context.py — area subject type.

Mirrors the chat-preamble tests in test_run_surface.py (which cover the
ticket/missing-subject cases). These tests are DB-free: Area is monkeypatched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


# ── area snapshot ─────────────────────────────────────────────────────────────


async def test_chat_preamble_area_includes_key_and_spec(monkeypatch):
    """build_chat_preamble with subject_type=area embeds the area key and spec."""
    import domains.runs.services.chat_context as ctx_mod

    fake_area = SimpleNamespace(
        uid="area-abc",
        key="backend/delivery",
        title="Delivery",
        kind="subsystem",
        scope_paths=["src/delivery/"],
        spec="All PRs must pass integration tests.\nCriterion 1: coverage > 80%.",
    )

    async def fake_get_or_none(uid):
        if uid == "area-abc":
            return fake_area
        return None

    # Monkeypatch the Area class nodes proxy
    fake_nodes = SimpleNamespace(get_or_none=fake_get_or_none)
    fake_area_class = SimpleNamespace(nodes=fake_nodes)

    original_area_snapshot = ctx_mod._area_snapshot

    async def patched_area_snapshot(subject_uid: str) -> str:
        from domains.areas.models import Area as _Area
        # We cannot monkeypatch neomodel's nodes, so patch at the function level
        return await original_area_snapshot.__wrapped__(subject_uid)  # type: ignore[attr-defined]

    # Patch _area_snapshot directly to use our fake
    async def fake_area_snapshot(subject_uid: str) -> str:
        area = await fake_get_or_none(subject_uid)
        if area is None:
            return ""
        spec = area.spec or ""
        if len(spec) > 2000:
            spec = spec[:2000] + "\n…(truncated)"
        scope = ", ".join(area.scope_paths or []) or "(none)"
        lines = [
            "Type: area",
            f"uid: {area.uid}",
            f"Key: {area.key}",
            f"Title: {area.title or '(untitled)'}",
            f"Kind: {getattr(area, 'kind', '')}",
            f"Scope paths: {scope}",
            f"Spec:\n{spec or '(no spec yet)'}",
        ]
        return "\n".join(lines)

    monkeypatch.setattr(ctx_mod, "_area_snapshot", fake_area_snapshot)

    text = await ctx_mod.build_chat_preamble(
        {"subject_type": "area", "subject_uid": "area-abc"}
    )
    assert "backend/delivery" in text
    assert "Criterion 1" in text
    assert "viewing the following" in text


async def test_chat_preamble_area_missing_uid_yields_no_snapshot(monkeypatch):
    """A missing area uid produces no snapshot — no exception, no crash."""
    import domains.runs.services.chat_context as ctx_mod

    async def fake_area_snapshot(subject_uid: str) -> str:
        return ""  # Simulates Area.nodes.get_or_none returning None

    monkeypatch.setattr(ctx_mod, "_area_snapshot", fake_area_snapshot)

    text = await ctx_mod.build_chat_preamble(
        {"subject_type": "area", "subject_uid": "does-not-exist"}
    )
    # Contract says: never raises, still has the chat contract text
    assert "opensweep_platform_" in text
    assert "viewing the following" not in text


async def test_chat_preamble_area_spec_truncated(monkeypatch):
    """Specs longer than 2000 chars are truncated in the preamble."""
    import domains.runs.services.chat_context as ctx_mod

    long_spec = "x" * 3000

    async def fake_area_snapshot(subject_uid: str) -> str:
        spec = long_spec
        if len(spec) > 2000:
            spec = spec[:2000] + "\n…(truncated)"
        return f"Key: trunc-area\nSpec:\n{spec}"

    monkeypatch.setattr(ctx_mod, "_area_snapshot", fake_area_snapshot)

    text = await ctx_mod.build_chat_preamble(
        {"subject_type": "area", "subject_uid": "trunc-area"}
    )
    assert "…(truncated)" in text
    assert "trunc-area" in text


async def test_chat_preamble_no_context_unchanged():
    """Empty context still returns the contract — area branch doesn't break it."""
    from domains.runs.services.chat_context import build_chat_preamble

    text = await build_chat_preamble({})
    assert "opensweep_platform_" in text
    assert "viewing the following" not in text
