"""Doc + DocEdit nodes — OpenSweep's curated documentation (KNOWLEDGE_V3).

Docs are the platform's briefing material about a repository: features,
architecture, and conventions. Human-owned; agents propose changes as
DocEdits. A page exists or is deleted — the only workflow state lives on
DocEdit (pending → accepted | rejected).

Docs are the platform's ONLY concept layer: slugs are path-like
("backend/queue-workers") and folders are derived from slug prefixes;
`watch_paths` anchors a page to the repository paths it documents, which
drives webhook staleness (§9) and is the anchor space for Memories and
Checked stamps (via the Doc uid).

Pinned pages are injected verbatim into every run's first prompt; unpinned
pages appear as a one-line index agents fetch on demand via read_doc.
"""

from domains.freshness import is_tracked, node_is_stale
from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)


class Doc(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # Stable identifier, unique per repository, path-like: "conventions",
    # "architecture", "backend/queue-workers". "/" segments form folders.
    slug = StringProperty(required=True, index=True)

    title = StringProperty(default="")
    # One line, shown in the prompt index for unpinned pages.
    summary = StringProperty(default="")
    body = StringProperty(default="")  # markdown

    pinned = BooleanProperty(default=False, index=True)

    # Retired page: kept for detail views/history but excluded from listings,
    # briefings, export, and audit selection (parity with AreaEdit retirement).
    # Applied on accept of a DocEdit whose proposed_archived is True.
    archived = BooleanProperty(default=False, index=True)

    # Repository path prefixes this page documents. The push webhook matches
    # changed paths against these to mark the page stale.
    watch_paths = JSONProperty(default=[])
    # Last push that touched watch_paths. Stale = code_changed_at newer than
    # last_reviewed_at (derived, never stored).
    code_changed_at = DateTimeProperty()
    # Advances when the page's BODY or watch_paths change (a human edit or an
    # accepted content DocEdit) or on confirm_doc_current. A rename, a summary
    # tweak, a pin, or an archive-on-accept deliberately does not: none of them
    # says the prose was re-checked against the code.
    last_reviewed_at = DateTimeProperty()
    # Changed paths accumulated since the last review; cleared on review.
    stale_paths = JSONProperty(default=[])

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


class DocEdit(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # "" = proposes a NEW page (slug/title/summary/watch_paths describe it).
    doc_uid = StringProperty(default="", index=True)
    slug = StringProperty(default="")
    title = StringProperty(default="")
    summary = StringProperty(default="")
    watch_paths = JSONProperty(default=[])

    # Full replacement markdown; the UI renders the diff against the current body.
    proposed_body = StringProperty(default="")
    rationale = StringProperty(default="")

    # Proposed archived state — True = the agent proposes RETIRING the page
    # (applied on human accept; mirrors AreaEdit.proposed_enabled).
    proposed_archived = BooleanProperty(default=False)

    source_run_uid = StringProperty(default="", index=True)

    status = StringProperty(default="pending", index=True)  # pending | accepted | rejected
    resolved_by = StringProperty(default="")
    resolved_at = DateTimeProperty()

    created_at = DateTimeProperty(default_now=True)


DOC_EDIT_STATUSES = {"pending", "accepted", "rejected"}

CONVENTIONS_SLUG = "conventions"


def doc_is_stale(d: Doc) -> bool:
    """Derived: code changed under watch_paths since the page was last
    reviewed. Never stored. The rule itself lives in `domains.freshness`."""
    return node_is_stale(d)


def doc_is_tracked(d: Doc) -> bool:
    """Does this page watch any path? An unwatched page can never go stale —
    not because it is current, but because nothing can mark it."""
    return is_tracked(d.watch_paths)
