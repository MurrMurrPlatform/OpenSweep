"""Area health rollup — the pure indexing helpers that make it one-pass.

The rollup exists to answer every area from a few indexes instead of rescanning
the tree and the stamp set per area, so the correctness of those indexes is
what these tests pin: prefix counting must respect the "/" boundary, and nested
scopes within one area must not double-count.
"""

from domains.areas.services.area_health import (
    _ancestors,
    _count_files,
    _prune_nested,
    _spec_state,
)


class _Area:
    def __init__(self, spec=""):
        self.spec = spec


# ---------- _ancestors ----------


def test_ancestors_walks_up_every_directory():
    assert _ancestors("a/b/c.py") == ["a/b/c.py", "a/b", "a"]


def test_ancestors_of_a_root_file_is_just_itself():
    assert _ancestors("README.md") == ["README.md"]


# ---------- _count_files ----------


def _tree(*paths):
    return sorted(paths)


def test_counts_files_under_a_directory_scope():
    tree = _tree("src/a.py", "src/b.py", "src/deep/c.py", "other/d.py")
    assert _count_files(tree, "src") == 3


def test_prefix_respects_the_slash_boundary():
    """The bug a bare startswith would introduce: "src-legacy" is not under
    "src". The count is bounded by the character right after "/"."""
    tree = _tree("src/a.py", "src-legacy/b.py", "srcx/c.py")
    assert _count_files(tree, "src") == 1


def test_a_scope_naming_a_single_file_counts_that_file():
    tree = _tree("src/a.py", "src/b.py")
    assert _count_files(tree, "src/a.py") == 1


def test_scope_matching_nothing_counts_zero():
    assert _count_files(_tree("src/a.py"), "does/not/exist") == 0


def test_empty_scope_counts_zero():
    assert _count_files(_tree("src/a.py"), "") == 0


# ---------- _prune_nested ----------


def test_nested_scopes_are_pruned_so_files_are_not_double_counted():
    assert _prune_nested(["src", "src/api"]) == ["src"]


def test_sibling_scopes_are_both_kept():
    assert _prune_nested(["src/api", "src/web"]) == ["src/api", "src/web"]


def test_duplicate_scopes_collapse():
    assert _prune_nested(["src", "src"]) == ["src"]


def test_prune_does_not_confuse_prefix_siblings():
    """"src-legacy" is not nested under "src" — both survive."""
    assert sorted(_prune_nested(["src", "src-legacy"])) == ["src", "src-legacy"]


def test_pruned_scopes_count_each_file_once():
    tree = _tree("src/a.py", "src/api/b.py", "src/api/c.py")
    scopes = _prune_nested(["src", "src/api"])
    assert sum(_count_files(tree, s) for s in scopes) == 3


# ---------- _spec_state ----------


def test_spec_state_missing_when_blank():
    assert _spec_state(_Area(""), False) == "missing"
    assert _spec_state(_Area("   \n "), False) == "missing"


def test_spec_state_stale_when_area_is_stale():
    """A spec written against code that has since moved is not a current spec —
    the same rule generate-specs already applies."""
    assert _spec_state(_Area("what to check"), True) == "stale"


def test_spec_state_present_when_written_and_current():
    assert _spec_state(_Area("what to check"), False) == "present"


def test_missing_beats_stale():
    """No spec at all is the more actionable fact — don't report it as stale."""
    assert _spec_state(_Area(""), True) == "missing"
