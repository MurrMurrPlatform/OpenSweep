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


def test_feature_spanning_two_subsystems_is_clean():
    assert (
        validate_area_edit(
            _edit(
                "checkout-flow",
                kind="feature",
                scope_paths=["back_end/checkout", "front_end/checkout"],
            ),
            [
                _area("backend", scope_paths=["back_end"]),
                _area("frontend", scope_paths=["front_end"]),
            ],
        )
        == []
    )


def test_feature_never_warned_about_scope_overlap():
    # Features overlay the partition: overlapping a subsystem's files is the
    # point, not a violation — the only feature warning is the span check.
    assert (
        validate_area_edit(
            _edit(
                "checkout-flow",
                kind="feature",
                scope_paths=["back_end/checkout", "front_end/checkout"],
            ),
            [
                _area("backend", scope_paths=["back_end"]),
                _area("frontend", scope_paths=["front_end"]),
                _area(
                    "payments-flow",
                    kind="feature",
                    scope_paths=["back_end/checkout"],
                ),
            ],
        )
        == []
    )


def test_feature_confined_to_one_subsystem_warns():
    (warning,) = validate_area_edit(
        _edit("checkout-flow", kind="feature", scope_paths=["back_end"]),
        [
            _area("backend", scope_paths=["back_end"]),
            _area("frontend", scope_paths=["front_end"]),
        ],
    )
    assert "spans only subsystem 'backend'" in warning


def test_feature_spanning_leaves_of_one_branch_still_warns():
    # Two leaves under the same top-level branch is still one subsystem —
    # the backend-only "feature" the span check exists to catch.
    (warning,) = validate_area_edit(
        _edit(
            "delivery-flow",
            kind="feature",
            scope_paths=["back_end/api", "back_end/jobs"],
        ),
        [
            _area("backend/api", scope_paths=["back_end/api"]),
            _area("backend/jobs", scope_paths=["back_end/jobs"]),
            _area("frontend", scope_paths=["front_end"]),
        ],
    )
    assert "spans only subsystem 'backend'" in warning


def test_feature_overlapping_no_subsystem_warns():
    (warning,) = validate_area_edit(
        _edit("ghost-flow", kind="feature", scope_paths=["nowhere"]),
        [_area("backend", scope_paths=["back_end"])],
    )
    assert "overlaps no subsystem leaf" in warning


def test_scopeless_feature_is_a_grouping_and_not_checked():
    assert (
        validate_area_edit(
            _edit("checkout", kind="feature", scope_paths=[]),
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


def test_feature_ignore_without_reason_warns():
    assert validate_area_edit(
        _edit("features/none", kind="feature_ignore", proposed_spec=" "), []
    ) == [
        "feature_ignore area without a reason — the spec should say why these "
        "files are not auditable"
    ]


def test_feature_ignore_is_held_to_its_own_axis():
    """It parks files on the FEATURE axis, so a subsystem leaf covering the
    same paths is not a collision — that is the whole point of two axes."""
    assert validate_area_edit(
        _edit("features/none", kind="feature_ignore", scope_paths=["uv.lock"],
              proposed_spec="lockfile"),
        [_area("vendored", kind="ignore", scope_paths=["uv.lock"])],
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
