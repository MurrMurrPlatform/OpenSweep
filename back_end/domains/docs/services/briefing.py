"""First-turn prompt briefing (KNOWLEDGE_V3_DOCUMENTATION.md §3).

One shared builder every dispatch path uses: pinned Docs verbatim, a
likely-relevant listing for the run's target (linked doc_uids plus pages
whose watch_paths overlap the target paths — docs are leads, not truth,
so they're offered for `read_doc`, never force-fed), a folder-grouped
one-line index of the remaining pages, and the repository's most relevant
memories. Prompt cost stays proportional to relevance — everything else is
on-demand through the tools.

Memories used to be injected ONLY for `target_doc_uids`, which meant the
majority of runs saw none at all: a repo-scoped audit, a deep scan, an
implement or a fix run targets paths (or nothing), not a Doc, so the loop
that writes memories fed nothing back into the loop that needs them. Every
run for a repository now gets memories; the run's scope only decides which
ones and in what order (see `_scope_query`), and a hard cap keeps a repo with
hundreds of memories from eating the prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domains.docs.models import Doc, doc_is_stale
from domains.memory.schemas import MemoryDTO
from domains.memory.services.memory_service import search_memory
from domains.repositories.services.path_matching import watches_path

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _index_line(d: Doc) -> str:
    line = f"- {d.slug}: {d.title or d.slug}"
    if d.summary:
        line += f" — {d.summary}"
    if doc_is_stale(d):
        line += " (code changed since last review)"
    return line


def _grouped_index(docs: list[Doc]) -> str:
    """Root pages first, then pages grouped by top-level folder."""
    root = [d for d in docs if "/" not in d.slug]
    folders: dict[str, list[Doc]] = {}
    for d in docs:
        if "/" in d.slug:
            folders.setdefault(d.slug.split("/", 1)[0], []).append(d)
    lines = [_index_line(d) for d in root]
    for folder in sorted(folders):
        lines.append(f"{folder}/")
        lines.extend(_index_line(d) for d in folders[folder])
    return "\n".join(lines)


def _watch_overlaps_target(watch_paths: list[str], target_paths: list[str]) -> bool:
    return any(
        watches_path([w], t) or watches_path([t], w)
        for w in watch_paths
        for t in target_paths
    )


#: Memory injection budget. These are hard caps, not targets: the briefing is
#: prepended to EVERY first-turn prompt, so an unbounded memory section would
#: tax every run on the repo forever and crowd out the pinned docs above it.
#: ~10 memories at ~600 chars is on the order of a thousand tokens — enough to
#: carry the facts a run keeps re-learning, small enough to stay affordable.
_TARGET_MEMORY_LIMIT = 6  # per target Doc — these are the most on-point
_REPO_MEMORY_LIMIT = 10  # scope-ranked, repo-wide
_MEMORY_CHAR_BUDGET = 6000
_MEMORY_BODY_CHARS = 600

#: Known-false-positive budget. Same reasoning as the memory caps: this rides
#: every first-turn prompt on the repo, so it has to stay cheap.
_DISMISSED_LIMIT = 15
_DISMISSED_TITLE_CHARS = 120


def _scope_query(target_paths: list[str], related_docs: list[Doc]) -> str:
    """A bag of words describing what this run is about, for memory ranking.

    There is no free-text description of a run's scope to search with, so it
    is reconstructed from what the dispatch actually carries: the target paths
    (which tokenise into module and directory names) and the titles/summaries
    of the docs already judged relevant to the target. A repo-wide run with no
    target yields "", which the ranker reads as "no scope" and answers with
    the freshest memories rather than with nothing.
    """
    parts = list(target_paths)
    for d in related_docs:
        parts.extend([d.slug or "", d.title or "", d.summary or ""])
    return " ".join(p for p in parts if p)


def _memory_lines(memories: list[MemoryDTO]) -> list[str]:
    """Render memories to prompt lines, stopping at the char budget.

    Bodies are clipped rather than dropped: a memory's first sentences carry
    the fact, and a truncated fact plus the tool hint above beats silently
    omitting the memory the run needed. `search_memory` returns the full body
    on demand.
    """
    lines: list[str] = []
    spent = 0
    for m in memories:
        stale = " (possibly stale — code changed since)" if m.possibly_stale else ""
        body = (m.body or "").strip()
        if len(body) > _MEMORY_BODY_CHARS:
            body = body[:_MEMORY_BODY_CHARS].rstrip() + "… (full text via search_memory)"
        line = f"- **{m.title}**{stale}: {body}".rstrip(": ")
        if spent + len(line) > _MEMORY_CHAR_BUDGET and lines:
            break
        spent += len(line)
        lines.append(line)
    return lines


async def _briefing_memories(
    *, repository_uid: str, target_doc_uids: list[str], scope_query: str
) -> list[MemoryDTO]:
    """The memories this run gets, most on-point first.

    Two passes, because they answer different questions. The anchored pass
    asks "what do we know about the exact Docs this run targets" — that is a
    scope, so it stays a hard filter. The repo-wide pass asks "what else do we
    know about this repository that touches this run's scope", ranked, and it
    is the one that fixes the regression: it runs even when there are no
    target docs, which is most runs.
    """
    memories: list[MemoryDTO] = []
    for anchor_uid in target_doc_uids:
        memories.extend(
            await search_memory(
                repository_uid=repository_uid,
                anchor_uid=anchor_uid,
                limit=_TARGET_MEMORY_LIMIT,
            )
        )
    memories.extend(
        await search_memory(
            repository_uid=repository_uid,
            query=scope_query,
            limit=_REPO_MEMORY_LIMIT,
        )
    )
    seen: set[str] = set()
    deduped: list[MemoryDTO] = []
    for m in memories:
        if m.uid in seen:
            continue
        seen.add(m.uid)
        deduped.append(m)
    return deduped


async def _dismissed_finding_lines(repository_uid: str) -> list[str]:
    """Recently dismissed findings, as one-line reminders. Best-effort.

    The platform stored the human verdict on every triaged finding and fed it
    into nothing an agent could see: `trust_score` sinks a dismissed finding
    in the RANKING, and the dedupe key suppresses a byte-identical re-report,
    but a rephrased title or a moved file produces a brand-new open finding at
    full trust. Worse, `list_findings` defaults to status=open, so the very
    findings an agent most needs to avoid re-filing were the ones it could not
    see. This closes that loop where it is cheapest — in the briefing every
    run already reads.
    """
    from domains.findings.models import Finding

    try:
        rows = [
            f
            for f in await Finding.nodes.filter(repository_uid=repository_uid)
            if (f.status or "") == "dismissed"
        ]
    except Exception:  # noqa: BLE001 — the briefing is never turn-fatal
        return []
    rows.sort(key=lambda f: f.updated_at or f.created_at or _EPOCH, reverse=True)
    out: list[str] = []
    for f in rows[:_DISMISSED_LIMIT]:
        title = (f.title or "").strip()[:_DISMISSED_TITLE_CHARS]
        if not title:
            continue
        paths = ", ".join(list(f.affected_paths or [])[:2])
        out.append(f"- {title}" + (f" ({paths})" if paths else ""))
    return out


async def build_briefing(
    *,
    repository_uid: str,
    target_doc_uids: list[str] | None = None,
    target_paths: list[str] | None = None,
) -> str:
    docs = [
        d
        for d in await Doc.nodes.all()
        if d.repository_uid == repository_uid and not d.archived
    ]
    docs.sort(key=lambda d: d.slug)
    target_uids = set(target_doc_uids or [])
    paths = [str(p) for p in (target_paths or []) if p]

    pinned = [d for d in docs if d.pinned]
    # Likely relevant: linked to the run's target (an area's doc_uids or an
    # explicit doc target) or watching code inside the target's paths.
    related = [
        d
        for d in docs
        if not d.pinned
        and (
            d.uid in target_uids
            or _watch_overlaps_target([str(w) for w in (d.watch_paths or [])], paths)
        )
    ]
    related_uids = {d.uid for d in related}
    indexed = [d for d in docs if not d.pinned and d.uid not in related_uids]

    sections: list[str] = []

    if pinned:
        pages = "\n\n".join(
            f"### {d.title or d.slug}\n\n{d.body}".strip()
            for d in pinned
            if (d.body or "").strip()
        )
        if pages:
            sections.append(f"## Repository documentation\n\n{pages}")

    if related:
        sections.append(
            "## Docs likely relevant to this run's scope\n\n"
            "Not exhaustive, and docs may trail the code — read the ones "
            "you need with read_doc and trust the code where they disagree.\n\n"
            + "\n".join(_index_line(d) for d in related)
        )

    if indexed:
        sections.append(
            "## Other documentation pages (fetch with read_doc)\n\n"
            + _grouped_index(indexed)
        )

    memories = await _briefing_memories(
        repository_uid=repository_uid,
        target_doc_uids=[uid for uid in (target_doc_uids or []) if uid],
        scope_query=_scope_query(paths, related),
    )
    lines = _memory_lines(memories)
    if lines:
        sections.append(
            "## What previous runs learned about this repository\n\n"
            "Facts earlier runs recorded that the code does not state. Not "
            "exhaustive and not authoritative — search_memory for more, and "
            "trust the code where they disagree.\n\n" + "\n".join(lines)
        )

    dismissed = await _dismissed_finding_lines(repository_uid)
    if dismissed:
        sections.append(
            "## Already judged NOT a problem here\n\n"
            "A human reviewed each of these and dismissed it. Do not re-file "
            "them. If you believe one is real after all, say so with the new "
            "evidence that changes the picture rather than filing it again "
            "as if it were fresh.\n\n" + "\n".join(dismissed)
        )

    return "\n\n".join(sections).strip()
