"""Pure tests for validate_area_edit — the accept-time advisory checks.

The function is warnings-only over (edit fields, {key, kind, scope_paths,
enabled} dicts): it flags partition smells for the reviewing human but
never blocks an accept.
"""

from types import SimpleNamespace

from domains.areas.services.area_service import validate_area_edit


def _edit(key, kind="subsystem", scope_paths=(), proposed_spec="a reason"):
    return SimpleNamespace(
        key=key, kind=kind, scope_paths=list(scope_paths), proposed_spec=proposed_spec
    )


def _area(key, kind="subsystem", scope_paths=(), enabled=True):
    return {
        "key": key,
        "kind": kind,
        "scope_paths": list(scope_paths),
        "enabled": enabled,
    }


def test_leaf_vs_leaf_equal_scope_warns():
    warnings = validate_area_edit(
        _edit("backend/api", scope_paths=["back_end/api"]),
        [_area("backend/jobs", scope_paths=["back_end/api"])],
    )
    assert warnings == ["scope 'back_end/api' overlaps leaf 'backend/jobs' ('back_end/api')"]


def test_leaf_vs_leaf_prefix_scope_warns_both_directions():
    # The edit's scope contains the other leaf's scope…
    assert validate_area_edit(
        _edit("backend/api", scope_paths=["back_end"]),
        [_area("backend/jobs", scope_paths=["back_end/jobs"])],
    )
    # …and vice versa; the "/" boundary still applies (back_end vs back_end2).
    assert validate_area_edit(
        _edit("backend/api", scope_paths=["back_end/api/v1"]),
        [_area("backend/jobs", scope_paths=["back_end/api"])],
    )
    assert not validate_area_edit(
        _edit("backend/api", scope_paths=["back_end2"]),
        [_area("backend/jobs", scope_paths=["back_end"])],
    )


def test_parent_child_key_relationship_is_exempt():
    # A parent legitimately spans its children — no warning either way.
    assert not validate_area_edit(
        _edit("backend", scope_paths=["back_end"]),
        [_area("backend/delivery", scope_paths=["back_end/domains/delivery"])],
    )
    assert not validate_area_edit(
        _edit("backend/delivery", scope_paths=["back_end/domains/delivery"]),
        [_area("backend", scope_paths=["back_end"])],
    )


def test_ignore_leaves_participate_in_overlap():
    warnings = validate_area_edit(
        _edit("vendored", kind="ignore", scope_paths=["third_party"]),
        [_area("backend", scope_paths=["third_party/patched"])],
    )
    assert len(warnings) == 1


def test_feature_edits_never_warn():
    assert (
        validate_area_edit(
            _edit("checkout-flow", kind="feature", scope_paths=["back_end"]),
            [_area("backend", scope_paths=["back_end"])],
        )
        == []
    )


def test_feature_areas_are_not_overlap_targets():
    assert (
        validate_area_edit(
            _edit("backend", scope_paths=["back_end"]),
            [_area("checkout-flow", kind="feature", scope_paths=["back_end"])],
        )
        == []
    )


def test_ignore_without_reason_warns():
    warnings = validate_area_edit(_edit("vendored", kind="ignore", proposed_spec="  "), [])
    assert warnings == [
        "ignore area without a reason — the spec should say why these files "
        "are not auditable"
    ]
    assert validate_area_edit(
        _edit("vendored", kind="ignore", proposed_spec="generated code"), []
    ) == []


def test_non_leaf_subsystem_does_not_warn_about_its_children():
    # "backend" has enabled children, so it is a grouping — its (spanning)
    # scope never collides with the leaves that actually own the files.
    assert (
        validate_area_edit(
            _edit("backend", scope_paths=["back_end"]),
            [
                _area("backend/api", scope_paths=["back_end/api"]),
                _area("backend/jobs", scope_paths=["back_end/jobs"]),
            ],
        )
        == []
    )


def test_disabled_areas_are_ignored():
    assert (
        validate_area_edit(
            _edit("backend/api", scope_paths=["back_end/api"]),
            [_area("backend/jobs", scope_paths=["back_end/api"], enabled=False)],
        )
        == []
    )


def test_non_leaf_others_are_not_overlap_targets():
    # "backend" is a grouping (it has an enabled child) — only its leaf child
    # can collide, and that child's scope doesn't.
    assert (
        validate_area_edit(
            _edit("frontend", scope_paths=["shared"]),
            [
                _area("backend", scope_paths=["shared"]),
                _area("backend/api", scope_paths=["back_end/api"]),
            ],
        )
        == []
    )
