"""Analysis authoring tools — wiring + DB-free validation.

The happy path touches Neo4j (get_or_create + save); here we assert the tools
are registered on every surface an agent reaches (dispatcher, MCP ops,
internal_llm prompt) and that input validation rejects bad args BEFORE any DB
write.
"""

import contextlib
import inspect
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.analysis.services.analysis_service import analysis_write_lock_key
from domains.platform_tools.add_analysis_note import add_analysis_note
from domains.platform_tools.ask_question import ask_question
from domains.platform_tools.dispatcher import tool_names
from domains.platform_tools.set_analysis_section import (
    _slugify_section,
    set_analysis_section,
)
from domains.platform_tools.upsert_analysis import upsert_analysis

ANALYSIS_TOOLS = {"upsert_analysis", "set_analysis_section", "add_analysis_note", "ask_question"}


def test_tools_registered_in_dispatcher():
    assert ANALYSIS_TOOLS <= set(tool_names())


def test_tools_registered_as_mcp_operations():
    from mcp_app import OPENSWEEP_PLATFORM_TOOL_OPERATIONS

    for t in ANALYSIS_TOOLS:
        assert f"opensweep_platform_{t}" in OPENSWEEP_PLATFORM_TOOL_OPERATIONS


def test_harness_prompts_list_the_tools():
    # Deep-scan runs execute on the harness executors, so both read prompts
    # must advertise the analysis tool surface.
    from domains.executors.prompt_kit import system_prompt

    for kind in ("claude_code_read", "cli_tracking"):
        prompt = system_prompt(kind)
        for t in ANALYSIS_TOOLS:
            assert t in prompt, f"{t} missing from {kind} prompt"


def test_http_routes_exist_for_each_tool():
    import api.v1.platform_tools as pt

    paths = {r.path for r in pt.router.routes}
    assert "/api/v1/platform-tools/upsert-analysis" in paths
    assert "/api/v1/platform-tools/set-analysis-section" in paths
    assert "/api/v1/platform-tools/add-analysis-note" in paths
    assert "/api/v1/platform-tools/ask-question" in paths


async def _expect_422(coro):
    with pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_upsert_rejects_bad_enums_before_db():
    await _expect_422(upsert_analysis(repository_uid="r", source_run_uid="s", status="nope"))
    await _expect_422(upsert_analysis(repository_uid="r", source_run_uid="s", health_grade="Z"))
    await _expect_422(upsert_analysis(repository_uid="r", source_run_uid="s", confidence="vibes"))


@pytest.mark.asyncio
async def test_note_rejects_bad_type_and_coverage_status():
    await _expect_422(add_analysis_note(repository_uid="r", source_run_uid="s", note_type="bogus"))
    await _expect_422(
        add_analysis_note(
            repository_uid="r", source_run_uid="s", note_type="coverage", status="halfway"
        )
    )


@pytest.mark.asyncio
async def test_question_and_section_reject_empty_before_db():
    await _expect_422(ask_question(repository_uid="r", source_run_uid="s", question="   "))
    await _expect_422(
        set_analysis_section(repository_uid="r", source_run_uid="s", section="  ", content="x")
    )


def test_section_slugify():
    assert _slugify_section("Executive Summary") == "executive_summary"
    assert _slugify_section("top-changes") == "top_changes"
    assert _slugify_section("  Weird!! Key ") == "weird_key"


def test_tool_signatures_key_off_source_run_uid():
    # Every authoring tool must accept repository_uid + source_run_uid (the
    # Analysis key injected from the run header).
    for fn in (upsert_analysis, set_analysis_section, add_analysis_note, ask_question):
        params = set(inspect.signature(fn).parameters)
        assert {"repository_uid", "source_run_uid"} <= params


# ── write-race lock (concurrent authoring tools cannot silently clobber) ─────


def test_lock_key_is_scoped_by_source_run_uid():
    # One Analysis per source_run_uid ⇒ one lock per source_run_uid: two runs
    # scanning in parallel must not queue behind each other.
    a = analysis_write_lock_key("run-a")
    b = analysis_write_lock_key("run-b")
    assert a != b
    assert "run-a" in a
    assert "run-b" in b


class _Saveable(SimpleNamespace):
    async def save(self):
        return self


def _wire_analysis_tool(monkeypatch, tool_module_name: str) -> dict:
    """Stub get_or_create + write_audit + capture the lock key each tool
    passes. Returns a dict populated on invocation with keys `lock_key`
    (str) and `node` (the fake node the tool mutated)."""
    mod = import_module(tool_module_name)
    captured: dict = {}

    async def fake_get_or_create(*, repository_uid, source_run_uid, executor="", revision=""):
        node = _Saveable(
            uid="an-1",
            repository_uid=repository_uid,
            source_run_uid=source_run_uid,
            executor=executor or "",
            revision=revision or "",
            title="",
            status="in_progress",
            completed_at=None,
            health_grade="",
            health_score=None,
            scorecard=[],
            confidence="",
            limitations="",
            stats={},
            sections={},
            coverage=[],
            strengths=[],
            validation_baseline=[],
            questions=[],
            updated_at=None,
        )
        captured["node"] = node
        return node

    @contextlib.asynccontextmanager
    async def fake_lock(source_run_uid):
        captured["lock_key"] = analysis_write_lock_key(source_run_uid)
        yield True

    async def fake_audit(**_):
        return None

    monkeypatch.setattr(mod, "get_or_create_analysis", fake_get_or_create)
    monkeypatch.setattr(mod, "analysis_write_lock", fake_lock)
    if hasattr(mod, "write_audit"):
        monkeypatch.setattr(mod, "write_audit", fake_audit)
    return captured


@pytest.mark.asyncio
async def test_upsert_analysis_holds_lock_around_write(monkeypatch):
    captured = _wire_analysis_tool(monkeypatch, "domains.platform_tools.upsert_analysis")
    await upsert_analysis(repository_uid="repo-1", source_run_uid="run-x", title="t")
    assert captured["lock_key"] == analysis_write_lock_key("run-x")
    assert captured["node"].title == "t"


@pytest.mark.asyncio
async def test_set_analysis_section_holds_lock_around_write(monkeypatch):
    captured = _wire_analysis_tool(monkeypatch, "domains.platform_tools.set_analysis_section")
    await set_analysis_section(
        repository_uid="repo-1",
        source_run_uid="run-x",
        section="Executive Summary",
        content="hello",
    )
    assert captured["lock_key"] == analysis_write_lock_key("run-x")
    assert captured["node"].sections == {"executive_summary": "hello"}


@pytest.mark.asyncio
async def test_add_analysis_note_holds_lock_around_write(monkeypatch):
    captured = _wire_analysis_tool(monkeypatch, "domains.platform_tools.add_analysis_note")
    await add_analysis_note(
        repository_uid="repo-1",
        source_run_uid="run-x",
        note_type="coverage",
        area="auth",
        status="examined",
    )
    assert captured["lock_key"] == analysis_write_lock_key("run-x")
    assert len(captured["node"].coverage) == 1


@pytest.mark.asyncio
async def test_ask_question_holds_lock_around_write(monkeypatch):
    captured = _wire_analysis_tool(monkeypatch, "domains.platform_tools.ask_question")
    await ask_question(
        repository_uid="repo-1", source_run_uid="run-x", question="Where is prod?"
    )
    assert captured["lock_key"] == analysis_write_lock_key("run-x")
    assert len(captured["node"].questions) == 1
