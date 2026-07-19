"""Draft + verify a documentation page against its code (KNOWLEDGE_V3).

The page IS the spec: its body holds the claims/acceptance criteria, its
`watch_paths` name the code that should satisfy them. Two operations:

* `draft_doc_page(doc_uid)` — dispatches one LLM run that reads the code
  at the page's watch_paths and proposes the page's full body via
  `propose_doc_edit`. The edit lands pending for human review.

* `verify_doc_page(doc_uid)` — dispatches one LLM run that checks each
  claim/criterion on the page against current code, filing
  `Finding(kind=defect)` for misses.

Both reuse `lifecycle.trigger_run`.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from domains.docs.models import Doc
from domains.runs.models import Run
from domains.runs.schemas import RunTrigger
from domains.runs.services.lifecycle import LifecycleError, trigger_run


def _format_doc_summary(doc: Doc) -> str:
    lines: list[str] = [
        f"slug: {doc.slug}",
        f"uid: {doc.uid}",
        f"title: {doc.title or doc.slug}",
        f"summary: {doc.summary or '(none)'}",
    ]
    if doc.watch_paths:
        lines.append(f"watch_paths: {', '.join(doc.watch_paths)}")
    return "\n".join(lines)


async def _get_doc(doc_uid: str) -> Doc:
    doc = await Doc.nodes.get_or_none(uid=doc_uid)
    if doc is None:
        raise LifecycleError(f"doc {doc_uid} not found")
    return doc


async def draft_doc_page(
    *,
    doc_uid: str,
    agent_uid: Optional[str] = None,
    triggered_by: str = "",
) -> Run:
    doc = await _get_doc(doc_uid)

    existing_note = (
        f"The page has a body already — read it with read_doc(slug={doc.slug}) "
        "and propose a full improved replacement."
        if (doc.body or "").strip()
        else "The page is empty — your propose_doc_edit creates its first body."
    )

    intent = _DRAFT_PAGE_INTENT.format(
        doc_summary=_format_doc_summary(doc),
        slug=doc.slug,
        doc_uid=doc.uid,
        existing_note=existing_note,
    )

    return await trigger_run(
        repository_uid=doc.repository_uid,
        intent=intent,
        playbook="document",
        title=f"Draft page — {doc.title or doc.slug}",
        target={"doc_uids": [doc.uid], "paths": list(doc.watch_paths or [])},
        trigger=RunTrigger.MANUAL,
        triggered_by=triggered_by or "draft-page",
    )


async def verify_doc_page(
    *,
    doc_uid: str,
    agent_uid: Optional[str] = None,
    triggered_by: str = "",
) -> Run:
    doc = await _get_doc(doc_uid)

    body = (doc.body or "").strip()
    if not body:
        raise LifecycleError(
            f"page {doc.slug!r} has no body to verify (draft and accept it first)"
        )
    if len(body) > 6000:
        body = body[:6000] + "…"

    intent = _VERIFY_PAGE_INTENT.format(
        doc_summary=_format_doc_summary(doc),
        slug=doc.slug,
        doc_body=body,
    )

    return await trigger_run(
        repository_uid=doc.repository_uid,
        intent=intent,
        playbook="ask",
        title=f"Verify — {doc.title or doc.slug}",
        target={"doc_uids": [doc.uid], "paths": list(doc.watch_paths or [])},
        trigger=RunTrigger.MANUAL,
        triggered_by=triggered_by or "verify-page",
    )


_DRAFT_PAGE_INTENT = """Draft this documentation page from the code it describes.

# Page

{doc_summary}

# Current state

{existing_note}

# Task

Read the code at the page's watch_paths and write the page: what this
part of the system does, how it works, and — for feature pages — 3-8
concise, machine-verifiable acceptance criteria (Given / When / Then is
encouraged but not required).

Call `propose_doc_edit` ONCE with:
- `slug={slug}`
- `title=<page title>`
- `summary=<one line: what this page covers>`
- `watch_paths=<the paths the page describes — refine if you found better ones>`
- `proposed_body=<the full page, markdown>`
- `rationale=<one sentence on what you based the page on>`

Every claim must be checkable against code you actually read. Do NOT
file ordinary Findings in this run. The output is the page only."""


_VERIFY_PAGE_INTENT = """Verify this documentation page against current code.

# Page

{doc_summary}

# Page body (slug={slug})

{doc_body}

# Task

For each claim / acceptance criterion above:
1. Read the code at the page's watch_paths.
2. Decide: does the current code satisfy it?
   - **Holds** → do nothing.
   - **Fails** → file `create_finding`:
     - `kind=defect`
     - `severity` reflecting impact (high if the behaviour is broken; medium if degraded)
     - `title=<concrete description of what's broken>`
     - `affected_paths=<specific file paths>`
     - `evidence.doc_slug={slug}`
     - `evidence.criterion=<the claim/criterion heading>`
     - `evidence.rationale=<why this code does not satisfy it>`
   - **Cannot decide** (insufficient evidence in code) → file
     `create_finding(kind=gap, severity=low, tags=["tests"],
     evidence.doc_slug={slug})` so the user knows it is unverifiable.

If every claim holds, call `confirm_doc_current(slug={slug})`. Do NOT
edit the page in this run.

Use `opensweep_search_findings` to check whether an open Finding already exists
for a given claim; if so, prefer `update_finding` to add fresh evidence
over filing a duplicate."""
