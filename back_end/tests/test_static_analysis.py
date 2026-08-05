"""Static-analysis candidate pipeline (§E) — pure parser/filter/render tests."""

import json
from pathlib import Path

from domains.execution.services.static_analysis import (
    ANALYZER_TOOLS,
    ANALYZERS,
    Candidate,
    AnalyzerReport,
    detect_default_tools,
    filter_candidates,
    render_candidates_section,
    resolve_tools,
    _parse_deptry,
    _parse_knip,
    _parse_ruff,
    _parse_semgrep,
    _parse_vulture,
)


# ── Parsers ──────────────────────────────────────────────────────────────────


def test_parse_ruff_maps_pyflakes_to_high():
    raw = json.dumps(
        [
            {"code": "F821", "filename": "app.py", "location": {"row": 3}, "message": "undefined name"},
            {"code": "E501", "filename": "app.py", "location": {"row": 9}, "message": "line too long"},
        ]
    )
    out = _parse_ruff(raw)
    assert [(c.rule, c.severity, c.line) for c in out] == [
        ("F821", "high", 3),
        ("E501", "medium", 9),
    ]
    assert _parse_ruff("not json") == []


def test_parse_vulture_lines():
    raw = "src/dead.py:12: unused function 'ghost' (60% confidence)\nnoise without location\n"
    out = _parse_vulture(raw)
    assert len(out) == 1
    assert out[0].path == "src/dead.py" and out[0].line == 12 and out[0].severity == "low"


def test_parse_deptry_entries():
    raw = json.dumps(
        [{"error": {"code": "DEP002", "message": "'leftpad' unused"}, "module": "leftpad", "location": {"file": "pyproject.toml", "line": 0}}]
    )
    out = _parse_deptry(raw)
    assert out[0].rule == "DEP002" and out[0].severity == "medium"


def test_parse_semgrep_severity_map():
    raw = json.dumps(
        {
            "results": [
                {"check_id": "exec-detected", "path": "a.py", "start": {"line": 4}, "extra": {"message": "exec()", "severity": "ERROR"}},
                {"check_id": "weak-hash", "path": "b.py", "start": {"line": 7}, "extra": {"message": "md5", "severity": "INFO"}},
            ]
        }
    )
    out = _parse_semgrep(raw)
    assert [c.severity for c in out] == ["high", "low"]


def test_parse_knip_files_and_exports():
    raw = json.dumps(
        {
            "files": ["src/orphan.ts"],
            "issues": [{"file": "src/util.ts", "exports": [{"name": "unusedFn", "line": 12}]}],
        }
    )
    out = _parse_knip(raw)
    assert {(c.rule, c.path) for c in out} == {
        ("unused-file", "src/orphan.ts"),
        ("unused-export", "src/util.ts"),
    }


# ── Detection & config resolution ────────────────────────────────────────────


def test_detect_default_tools_by_ecosystem(tmp_path):
    assert detect_default_tools(str(tmp_path)) == []
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert [t["tool"] for t in detect_default_tools(str(tmp_path))] == ["ruff", "vulture", "deptry"]
    (tmp_path / "package.json").write_text("{}")
    assert "knip" in [t["tool"] for t in detect_default_tools(str(tmp_path))]
    # semgrep is never auto-enabled — it needs an explicit ruleset.
    assert "semgrep" not in [t["tool"] for t in detect_default_tools(str(tmp_path))]


def test_resolve_tools_honors_mode(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert resolve_tools({"mode": "off"}, str(tmp_path)) == []
    custom = {"mode": "custom", "tools": [{"tool": "semgrep"}, {"tool": "bogus"}]}
    assert [t["tool"] for t in resolve_tools(custom, str(tmp_path))] == ["semgrep"]
    assert [t["tool"] for t in resolve_tools(None, str(tmp_path))] == ["ruff", "vulture", "deptry"]


def test_registry_covers_all_documented_tools():
    assert set(ANALYZERS) == {"ruff", "vulture", "deptry", "semgrep", "knip"}


# ── Scope filter & rendering ─────────────────────────────────────────────────


def _cand(path, severity="medium", tool="ruff"):
    return Candidate(tool=tool, rule="r", path=path, line=1, message="m", severity=severity)


def test_filter_candidates_by_path_prefix():
    cands = [_cand("back_end/app.py"), _cand("front_end/x.ts")]
    kept = filter_candidates(cands, allowed_paths=["back_end/"])
    assert [c.path for c in kept] == ["back_end/app.py"]
    assert filter_candidates(cands, allowed_paths=None) == cands


def test_render_caps_and_notes_truncation_and_skips():
    report = AnalyzerReport(
        candidates=[_cand(f"f{i}.py", severity="low") for i in range(10)]
        + [_cand("hot.py", severity="high")],
        tools_run=["ruff"],
        tools_skipped=[{"tool": "knip", "reason": "not installed"}],
    )
    section = render_candidates_section(report, cap=5, artifact_uri="opensweep-artifact://r/x/static_analysis.json")
    assert "CANDIDATE, not a confirmed problem" in section
    assert section.count("\n- ") == 5
    # first rendered candidate line — severity-desc ordering puts the high one first
    first_candidate = next(ln for ln in section.splitlines() if ln.startswith("- "))
    assert "[high] ruff" in first_candidate
    assert "5 of 11 candidates shown" in section
    assert "skipped: knip — not installed" in section


def test_render_empty_report_is_silent():
    assert render_candidates_section(AnalyzerReport(), cap=5) == ""
    clean = AnalyzerReport(tools_run=["ruff"])
    assert "ran clean" in render_candidates_section(clean, cap=5)


# ── Every advertised analyzer must actually be installed in the images ──────

_BACK_END = Path(__file__).resolve().parent.parent

# ruff/vulture/deptry/semgrep are pip packages resolved via requirements.txt;
# knip is the one npm package (`ANALYZER_TOOLS` binary name doubles as its
# package name for all five, so this maps 1:1 with no per-tool table).
_PIP_TOOLS = {"ruff", "vulture", "deptry", "semgrep"}
_NPM_TOOLS = {"knip"}


def test_pip_analyzer_tools_are_in_requirements():
    assert _PIP_TOOLS | _NPM_TOOLS == set(ANALYZER_TOOLS), (
        "a new tool was added to ANALYZER_TOOLS without updating this test's "
        "pip/npm split"
    )
    requirements = (_BACK_END / "requirements.txt").read_text()
    missing = [t for t in _PIP_TOOLS if t not in requirements]
    assert not missing, f"{missing} missing from requirements.txt — shutil.which() will never find them"


def test_npm_analyzer_tools_are_installed_in_both_docker_images():
    for dockerfile in ("Dockerfile.dev", "Dockerfile.prod"):
        contents = (_BACK_END / dockerfile).read_text()
        missing = [t for t in _NPM_TOOLS if t not in contents]
        assert not missing, f"{missing} missing from {dockerfile} — shutil.which() will never find them"
