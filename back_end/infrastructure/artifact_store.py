"""Raw-artifact retention for Runs.

PLATFORM.md §Executor return contracts is firm: raw executor output is
always retained, regardless of whether tool-call / structured-blob /
raw-transcript path succeeded. This service is the named home for those
blobs.

v1 backing: filesystem under settings.ARTIFACT_STORE_ROOT (default
`var/artifacts/`). Layout:

  <root>/<repository_uid>/<run_uid>/<artifact_type>.<ext>

Each `put()` returns a `opensweep-artifact://...` URI that round-trips
through `get()`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

from config import settings
from logging_config import logger

_SCHEME = "opensweep-artifact"


def _root() -> Path:
    root = getattr(settings, "ARTIFACT_STORE_ROOT", None) or "var/artifacts"
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]


def put(
    *,
    repository_uid: str,
    run_uid: str,
    content: bytes | str,
    artifact_type: str = "raw",
    extension: str = "txt",
    summary: str = "",
) -> str:
    """Persist a blob and return its `opensweep-artifact://` URI."""
    repo_dir = _root() / _safe(repository_uid or "_unknown") / _safe(run_uid or uuid4().hex)
    repo_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe(artifact_type)}.{_safe(extension)}"
    path = repo_dir / filename
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    if summary:
        (repo_dir / f"{_safe(artifact_type)}.summary.txt").write_text(summary, encoding="utf-8")
    uri = f"{_SCHEME}://{repository_uid}/{run_uid}/{filename}"
    logger.debug(f"Artifact stored: {uri} ({len(data)} bytes)", extra={"tag": "artifact_store"})
    return uri


def _path_from_uri(uri: str) -> Optional[Path]:
    prefix = f"{_SCHEME}://"
    if not uri.startswith(prefix):
        return None
    rest = uri[len(prefix):]
    parts = rest.split("/", 2)
    if len(parts) != 3:
        return None
    return _root() / _safe(parts[0]) / _safe(parts[1]) / _safe(parts[2])


def repository_uid_of(uri: str) -> str:
    """The repository uid an artifact URI resolves to (F8).

    Applies the SAME `_safe` normalization the on-disk path uses, so the org
    check in the artifacts route can never diverge from the segment that
    actually addresses the blob. Empty for a non-artifact URI."""
    prefix = f"{_SCHEME}://"
    if not uri.startswith(prefix):
        return ""
    parts = uri[len(prefix):].split("/")
    return _safe(parts[0]) if parts and parts[0] else ""


def get(uri: str) -> Optional[bytes]:
    p = _path_from_uri(uri)
    if not p or not p.exists():
        return None
    return p.read_bytes()


def exists(uri: str) -> bool:
    p = _path_from_uri(uri)
    return bool(p and p.exists())


def delete(uri: str) -> bool:
    p = _path_from_uri(uri)
    if not p or not p.exists():
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False
