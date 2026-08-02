"""Structural facts from the code graph.

The graph was indexed on every clone, exposed to agents over MCP, and read by
the platform never — so "is there an import cycle" was an LLM reading files
one context window at a time, with the answer sitting in a SQLite index we had
already built.

The analysis lives in Python rather than in the binary's query language on
purpose: `query_graph` speaks a Cypher SUBSET where property-to-property
WHERE is a parse error and path queries don't exist, and a silently rejected
query would read as "no problems found".
"""

from infrastructure.code_graph_metrics import (
    _clean_edges,
    coupling_hotspots,
    god_modules,
    import_cycles,
    render_section,
)


# ── Edge hygiene ─────────────────────────────────────────────────────────


def test_self_edges_and_blanks_are_dropped():
    """A file importing itself is an indexer artifact, and it would otherwise
    make every file a one-node 'cycle'."""
    assert _clean_edges([["a.py", "a.py"], ["", "b.py"], ["c.py", ""], ["a.py", "b.py"]]) == [
        ("a.py", "b.py")
    ]


def test_short_rows_are_ignored_rather_than_raising():
    assert _clean_edges([["only-one"], [], ["a", "b"]]) == [("a", "b")]


# ── Cycles ───────────────────────────────────────────────────────────────


def test_a_two_file_cycle_is_found():
    assert import_cycles([("a.py", "b.py"), ("b.py", "a.py")]) == [["a.py", "b.py"]]


def test_a_longer_cycle_is_found():
    edges = [("a.py", "b.py"), ("b.py", "c.py"), ("c.py", "a.py")]
    assert import_cycles(edges) == [["a.py", "b.py", "c.py"]]


def test_an_acyclic_graph_reports_nothing():
    edges = [("a.py", "b.py"), ("b.py", "c.py"), ("a.py", "c.py")]
    assert import_cycles(edges) == []


def test_a_shared_dependency_is_not_a_cycle():
    """Two files importing the same helper is the normal case; reporting it
    would bury the real cycles."""
    assert import_cycles([("a.py", "util.py"), ("b.py", "util.py")]) == []


def test_cycles_are_reported_smallest_first():
    """A two-file cycle is a concrete, fixable statement; a large one is
    usually the indexer resolving a package to its own submodules."""
    edges = [
        ("a.py", "b.py"),
        ("b.py", "a.py"),
        ("x.py", "y.py"),
        ("y.py", "z.py"),
        ("z.py", "x.py"),
    ]
    assert [len(c) for c in import_cycles(edges)] == [2, 3]


def test_oversized_cycles_are_dropped():
    ring = [(f"f{i}.py", f"f{(i + 1) % 20}.py") for i in range(20)]
    assert import_cycles(ring, max_len=12) == []
    assert len(import_cycles(ring, max_len=25)) == 1


def test_a_deep_chain_does_not_blow_the_stack():
    """Iterative Tarjan: a recursive one would hit the recursion limit on a
    large repo, and this must never be the thing that fails a run."""
    chain = [(f"f{i}.py", f"f{i + 1}.py") for i in range(5000)]
    assert import_cycles(chain) == []


# ── Coupling ─────────────────────────────────────────────────────────────


def test_hotspots_count_distinct_dependents_not_edges():
    """One view importing the same component five times is ONE coupling
    relationship, not five."""
    edges = [("view.vue", "ui.ts")] * 5 + [("other.vue", "ui.ts")]
    assert coupling_hotspots(edges) == [("ui.ts", 2)]


def test_hotspots_are_ranked_and_capped():
    edges = [(f"src{i}.py", "hub.py") for i in range(5)] + [("a.py", "cold.py")]
    assert coupling_hotspots(edges, top=1) == [("hub.py", 5)]


# ── God modules ──────────────────────────────────────────────────────────


def test_god_modules_rank_by_definition_count():
    rows = [["small.py", 3], ["big.py", 90], ["mid.py", 40]]
    assert god_modules(rows, top=2) == [("big.py", 90), ("mid.py", 40)]


def test_unparseable_counts_are_skipped():
    assert god_modules([["a.py", "many"], ["b.py", 5]]) == [("b.py", 5)]


# ── Rendering ────────────────────────────────────────────────────────────


def test_nothing_to_say_renders_nothing():
    """An empty section would still cost prompt budget and imply a check ran
    and found the repo clean."""
    assert render_section([], [], []) == ""


def test_the_section_frames_facts_as_candidates_not_findings():
    section = render_section([["a.py", "b.py"]], [("hub.py", 9)], [("big.py", 90)])
    assert "FACTS about shape, not findings" in section
    # A hub module with many dependents is frequently correct design.
    assert "often" in section and "exactly right" in section
    assert "a.py → b.py → a.py" in section
    assert "hub.py — imported by 9 file(s)" in section
    assert "big.py — defines 90 symbols" in section
