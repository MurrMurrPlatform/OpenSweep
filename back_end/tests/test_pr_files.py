"""GitHub `pulls/{n}/files` payload → run-changes shape (PR Files panel) — pure mapping."""

from types import SimpleNamespace

from domains.delivery.services.pull_request_service import (
    PR_PATCH_MAX_CHARS,
    github_files_to_changes,
)

PR = SimpleNamespace(base_ref="main")


def _payload(**overrides):
    base = {
        "filename": "src/app.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n context",
    }
    base.update(overrides)
    return base


def test_maps_core_fields_and_base():
    out = github_files_to_changes(PR, [_payload()])
    assert out["source"] == "live"
    assert out["base"] == "main"
    assert out["tree"] == []
    (f,) = out["files"]
    assert f["path"] == "src/app.py"
    assert f["status"] == "modified"
    assert f["additions"] == 1 and f["deletions"] == 1
    assert f["patch"].startswith("@@")
    assert not f["binary"] and not f["too_large"]


def test_status_vocabulary_maps_to_run_changes():
    payloads = [
        _payload(filename="a", status="added"),
        _payload(filename="b", status="removed"),
        _payload(filename="c", status="renamed", previous_filename="old/c"),
        _payload(filename="d", status="copied"),
        _payload(filename="e", status="changed"),
        _payload(filename="f", status="mystery"),
    ]
    by_path = {f["path"]: f for f in github_files_to_changes(PR, payloads)["files"]}
    assert by_path["a"]["status"] == "added"
    assert by_path["b"]["status"] == "deleted"
    assert by_path["c"]["status"] == "renamed"
    assert by_path["c"]["old_path"] == "old/c"
    assert by_path["d"]["status"] == "added"
    assert by_path["e"]["status"] == "modified"
    assert by_path["f"]["status"] == "modified"


def test_missing_patch_with_counts_is_too_large():
    # GitHub omits `patch` for oversized text diffs but still counts lines.
    (f,) = github_files_to_changes(PR, [_payload(patch=None, additions=9000, deletions=1)])["files"]
    assert f["too_large"] and not f["binary"]


def test_missing_patch_without_counts_is_binary():
    (f,) = github_files_to_changes(PR, [_payload(patch=None, additions=0, deletions=0)])["files"]
    assert f["binary"] and not f["too_large"]


def test_pure_rename_without_patch_is_neither():
    (f,) = github_files_to_changes(
        PR,
        [_payload(status="renamed", patch=None, additions=0, deletions=0, previous_filename="x")],
    )["files"]
    assert not f["binary"] and not f["too_large"]


def test_oversized_patch_is_dropped_defensively():
    huge = "@@ -1 +1 @@\n" + "+x\n" * (PR_PATCH_MAX_CHARS // 3 + 1)
    (f,) = github_files_to_changes(PR, [_payload(patch=huge)])["files"]
    assert f["too_large"] and f["patch"] == ""


def test_files_sorted_and_nameless_skipped():
    payloads = [_payload(filename="z.py"), _payload(filename=""), _payload(filename="a.py")]
    out = github_files_to_changes(PR, payloads)
    assert [f["path"] for f in out["files"]] == ["a.py", "z.py"]
