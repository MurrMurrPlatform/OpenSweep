"""Seeded audit lenses — the platform's decomposed audit vocabulary.

The bodies decompose the monolithic audit prose of the seeded variants
(seed_variants.py: deep-issue-hunt, security-audit, review-adversarial)
into per-discipline snippets an area run can work one at a time. Each body
is standalone: what to check, what evidence a finding needs, and that
"checked, nothing found" is a valid verdict — the lens_verdicts contract
depends on agents saying so instead of padding.

Upsert semantics are byte-for-byte the prompt library's
(platform_prompts.upsert_platform_prompt): platform-owned fields are hashed
into `seed_checksum`, SYNC rolls forward provably untouched rows only,
FORCE resets everything, UPSERT never touches an existing row.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from domains.agents.services.platform_prompts import tally
from domains.lenses.models import Lens
from infrastructure.seeding.base import SeedMode, SeedResult, content_hash
from logging_config import logger

# Fields the platform owns and a SYNC is allowed to roll forward. Anything not
# listed (enabled, provenance, created_at, uid …) is never touched on re-seed.
_OWNED_FIELDS = (
    "title",
    "body",
    "tags",
    "wants",
    "global_agent_key",
)


# key → spec. Local lenses compose into area-run checklists
# (lens_service.lens_checklist); global lenses back whole-repo sweep agents
# via their global_agent_key variant slug.
_LENSES: dict[str, dict[str, Any]] = {
    "bugs": {
        "title": "Bugs",
        "tags": ["audit", "correctness"],
        "wants": ["static_analysis"],
        "body": (
            "Hunt for correctness defects in this scope.\n"
            "\n"
            "- Trace data from every external input (HTTP, queue, file, env, DB) to\n"
            "  the sensitive operations it reaches; at each boundary ask what happens\n"
            "  on empty/huge/malformed/concurrent/duplicate input.\n"
            "- Check the edges: null/empty handling, off-by-one, error paths that\n"
            "  leave partial state behind, regressions to existing callers.\n"
            "- Compare recently changed code against how the rest of the codebase\n"
            "  solves the same problem — divergence from an established pattern is\n"
            "  where bugs cluster. When a pattern proves buggy in one place, grep\n"
            "  this scope for its siblings before moving on.\n"
            "\n"
            "A finding needs concrete evidence: file paths, the failing scenario, and\n"
            "the mechanism of failure. Do not file style nits. \"Checked, nothing\n"
            "found\" is a valid verdict — report it rather than inventing problems."
        ),
    },
    "security": {
        "title": "Security",
        "tags": ["audit", "security"],
        "wants": ["static_analysis"],
        "body": (
            "Hunt for vulnerabilities with a real attack path in this scope.\n"
            "\n"
            "- Injection: SQL/Cypher/command/template injection, unsafe deserialization.\n"
            "- AuthN/AuthZ: missing or bypassable SERVER-side checks, privilege\n"
            "  escalation, IDOR, insecure session handling.\n"
            "- Crypto & secrets: hardcoded credentials, weak algorithms, secrets in logs.\n"
            "- Data exposure: PII in logs or error messages, missing access control.\n"
            "- Code execution: eval/exec on tainted input, pickle, path traversal on writes.\n"
            "\n"
            "Evidence must include a concrete exploit scenario: who the attacker is,\n"
            "what they send or do, and what they gain — if you cannot sketch an attack\n"
            "path, it is not a finding. Do NOT file denial-of-service, missing rate\n"
            "limiting, \"lack of hardening\" without an exploit, or attacks requiring\n"
            "control of the environment (env vars, CLI flags, local config) — those\n"
            "are trusted inputs. \"Checked, nothing found\" is a valid verdict."
        ),
    },
    "simplification": {
        "title": "Simplification",
        "tags": ["audit", "cleanup"],
        "wants": ["static_analysis"],
        "body": (
            "Hunt for code that could be smaller with identical behavior.\n"
            "\n"
            "- Dead code: unused functions, exports, and dependencies; commented-out\n"
            "  blocks guarding nothing; branches no input can reach.\n"
            "- Duplicate logic: the same problem solved twice where one\n"
            "  implementation could serve both call sites.\n"
            "- Over-engineering: abstraction without a second caller, config nothing\n"
            "  sets, flexibility nothing uses, indirection that only forwards.\n"
            "\n"
            "File kind=improvement with the paths involved and the simpler shape;\n"
            "name the callers/usages you checked so the finding is safe to act on\n"
            "without re-investigation. Do not file taste-only restructurings — the\n"
            "current code must carry a concrete cost (size, confusion, drift risk).\n"
            "\"Checked, nothing found\" is a valid verdict."
        ),
    },
    "refactor-opportunities": {
        "title": "Refactor opportunities",
        "tags": ["audit", "structure"],
        "wants": [],
        "body": (
            "Hunt for structural debt worth a deliberate refactor.\n"
            "\n"
            "- Divergence: code solving a problem differently from the repo's\n"
            "  established pattern for it.\n"
            "- Coupling: modules reaching into each other's internals, cyclic\n"
            "  imports, logic living in the wrong layer.\n"
            "- God functions/classes: units doing several jobs with visible seams.\n"
            "- Shotgun edits: changes that always touch the same N files together.\n"
            "\n"
            "For each finding, name the target shape and why it pays for itself (bug\n"
            "clusters, change friction, onboarding cost) — a refactor finding without\n"
            "a concrete payoff is noise. One finding per root cause listing the\n"
            "affected paths, not one per symptom site. \"Checked, nothing found\" is\n"
            "a valid verdict."
        ),
    },
    "test-gaps": {
        "title": "Test gaps",
        "tags": ["audit", "testing"],
        "wants": [],
        "body": (
            "Hunt for missing or hollow test coverage in this scope.\n"
            "\n"
            "- Untested behavior: error paths, boundary values, concurrency-sensitive\n"
            "  sections, and recently changed logic first — this is not\n"
            "  coverage-percent chasing.\n"
            "- Hollow tests: tests that would still pass if the behavior were wrong\n"
            "  or reverted, that assert nothing meaningful, or that are\n"
            "  skipped/disabled without a stated reason.\n"
            "- Failing tests: where the repo's own test commands are discoverable and\n"
            "  cheap to run, run them — a failing suite is an evidenced finding.\n"
            "\n"
            "Evidence: name the untested path and the scenario that would slip\n"
            "through (inputs, expected vs actual outcome). Do not file gaps on\n"
            "trivial getters or wiring. \"Checked, nothing found\" is a valid verdict."
        ),
    },
    "error-handling": {
        "title": "Error handling",
        "tags": ["audit", "reliability"],
        "wants": [],
        "body": (
            "Hunt for failure paths that lie or leave damage behind.\n"
            "\n"
            "- Swallowed errors: bare/broad excepts, errors logged and dropped where\n"
            "  the caller needed to know, silent fallbacks masking real failures.\n"
            "- Partial state: multi-step operations whose mid-way failure leaves\n"
            "  stores inconsistent — no rollback, no idempotent retry.\n"
            "- Missing observability: new failure modes with no log or metric, error\n"
            "  messages too vague to debug from.\n"
            "- Wrong-direction handling: retrying non-retryable errors, catching\n"
            "  where the fix belongs upstream.\n"
            "\n"
            "Evidence: the file path, the failure that triggers it, and what the user\n"
            "or operator sees versus what they should. Deliberate best-effort\n"
            "handling with a stated reason is not a finding. \"Checked, nothing\n"
            "found\" is a valid verdict."
        ),
    },
    "performance": {
        "title": "Performance",
        "tags": ["audit", "performance"],
        "wants": [],
        "body": (
            "Hunt for performance hazards with a plausible hot path.\n"
            "\n"
            "- N+1 queries and per-item I/O inside loops that could batch.\n"
            "- Unbounded work: missing pagination or limits, loops and allocations\n"
            "  growing with user-controlled input.\n"
            "- Blocking I/O on hot paths: sync calls inside async handlers, network\n"
            "  round-trips inside request loops.\n"
            "- Repeated recomputation of stable values that could be cached at a\n"
            "  clear invalidation boundary.\n"
            "\n"
            "Evidence: the code path, why it is (or will become) hot, and the growth\n"
            "factor — \"could be faster\" without a workload is not a finding. Do not\n"
            "file micro-optimizations on cold paths. \"Checked, nothing found\" is a\n"
            "valid verdict."
        ),
    },
    "legacy-patterns": {
        "title": "Legacy patterns",
        "tags": ["audit", "cleanup"],
        "wants": [],
        "body": (
            "Hunt for outdated idioms the codebase has already moved past.\n"
            "\n"
            "- Deprecated API and dependency usages the rest of the repo migrated\n"
            "  off, and half-finished migrations where old and new mechanisms live\n"
            "  side by side.\n"
            "- Superseded conventions: modules still on a naming/structure/error\n"
            "  style the newer code abandoned.\n"
            "- Compatibility shims and feature flags whose reason has expired.\n"
            "- Commented-out code and TODO/FIXME markers guarding real behavior with\n"
            "  no linked ticket.\n"
            "\n"
            "Evidence: the legacy site, its current-pattern counterpart elsewhere in\n"
            "the repo, and what the migration touches. Do not file where the \"old\n"
            "way\" is still the repo's dominant working convention. \"Checked,\n"
            "nothing found\" is a valid verdict."
        ),
    },
    # ── Global lenses — whole-repo sweeps, dispatched as their own agents ──
    "architecture-review": {
        "title": "Architecture review",
        "tags": ["audit", "architecture", "global"],
        "wants": ["static_analysis"],
        "global_agent_key": "architecture-review",
        "body": (
            "Whole-repo architecture audit — judge the shape of the system, not one\n"
            "area.\n"
            "\n"
            "First check open findings tagged escalate:architecture-review: verify\n"
            "each against the current code and expand or dismiss it with evidence.\n"
            "\n"
            "Then sweep for: module boundaries that leak (internals imported across\n"
            "domains), dependency direction violations (lower layers importing\n"
            "upward), god modules accumulating unrelated responsibilities, coupling\n"
            "hotspots every change touches, and the same problem solved with\n"
            "competing patterns across the codebase.\n"
            "\n"
            "Findings must be evidence-backed: the modules/paths involved, the\n"
            "boundary or dependency violated, and the concrete cost. \"Checked,\n"
            "nothing found\" is a valid verdict."
        ),
    },
    "implementation-gaps": {
        "title": "Implementation gaps",
        "tags": ["audit", "product", "global"],
        "wants": [],
        "global_agent_key": "implementation-gaps",
        "body": (
            "Whole-repo promise-vs-reality audit: compare what the product, docs,\n"
            "and README claim against what the code implements.\n"
            "\n"
            "First check open findings tagged escalate:implementation-gaps: verify\n"
            "each against the current code and expand or dismiss it with evidence.\n"
            "\n"
            "Then sweep for: advertised features that are stubs or TODO paths, dead\n"
            "feature flags nothing enables, flows that dead-end halfway (a UI\n"
            "without its backend, an API without a consumer), and missing error\n"
            "paths on features the docs promise are handled.\n"
            "\n"
            "Every finding cites both sides: where the promise is made (doc, README,\n"
            "UI copy) and where the code falls short. \"Checked, nothing found\" is a\n"
            "valid verdict."
        ),
    },
}


def _normalized(spec: dict[str, Any]) -> dict[str, Any]:
    """Fill a partial seed spec with the model defaults for owned fields, so
    the checksum is computed over the exact values that get written."""
    return {
        "title": spec.get("title", ""),
        "body": spec.get("body", ""),
        "tags": list(spec.get("tags", [])),
        "wants": list(spec.get("wants", [])),
        "global_agent_key": spec.get("global_agent_key", ""),
    }


def _checksum(values: dict[str, Any]) -> str:
    return content_hash(
        values["title"],
        values["body"],
        # list order is meaningful to the hash but not to behavior; the specs
        # list entries deterministically so this is stable.
        ",".join(values["tags"]),
        ",".join(values["wants"]),
        values["global_agent_key"],
    )


def _current_values(row: Lens) -> dict[str, Any]:
    return {
        "title": row.title or "",
        "body": row.body or "",
        "tags": list(row.tags or []),
        "wants": list(row.wants or []),
        "global_agent_key": row.global_agent_key or "",
    }


def _apply(row: Lens, values: dict[str, Any]) -> None:
    for f in _OWNED_FIELDS:
        setattr(row, f, values[f])
    row.seed_checksum = _checksum(values)


async def upsert_lens(
    spec: dict[str, Any],
    key: str,
    mode: SeedMode,
    *,
    existing: Optional[Lens] = None,
) -> str:
    """Create or reconcile one system Lens. Returns the action taken:
    "created" | "updated" | "unchanged" | "preserved".

    Decision table is identical to platform_prompts.upsert_platform_prompt —
    keyed by `key` instead of source_url."""
    values = _normalized(spec)
    shipped = _checksum(values)

    if existing is None:
        existing = next(
            iter(await Lens.nodes.filter(provenance="system", key=key)), None
        )

    if existing is None:
        row = Lens(
            uid=uuid4().hex,
            key=key,
            provenance="system",
            enabled=True,
            **values,
            seed_checksum=shipped,
        )
        await row.save()
        return "created"

    if mode is SeedMode.UPSERT:
        return "unchanged"

    current = _checksum(_current_values(existing))

    if mode is SeedMode.FORCE:
        # Overwrite to shipped regardless of who last touched the row. Compare
        # CURRENT content (not the stored checksum, which a user edit via the
        # API leaves stale) so a user edit that happens to match the row's old
        # seed hash is still reset.
        if current == shipped:
            if existing.seed_checksum != shipped:
                existing.seed_checksum = shipped
                await existing.save()
            return "unchanged"
        _apply(existing, values)
        await existing.save()
        return "updated"

    # SYNC: roll forward only rows we can prove the user hasn't edited.
    stored = existing.seed_checksum or ""
    if stored == "":
        # Untracked (hand-created or pre-checksum). We can only adopt it as
        # platform-owned when it already matches the shipped content —
        # otherwise we cannot tell a stale default from a user edit.
        if current == shipped:
            existing.seed_checksum = shipped
            await existing.save()
            return "unchanged"
        return "preserved"
    if stored != current:
        return "preserved"  # user edited since our last seed
    if current == shipped:
        return "unchanged"
    _apply(existing, values)
    await existing.save()
    return "updated"


async def seed_lenses(mode: SeedMode = SeedMode.UPSERT) -> SeedResult:
    """Ensure the lens library exists. Idempotent; mode governs re-seed of
    existing rows (see SeedMode / upsert_lens)."""
    by_key = {
        (lens.key or ""): lens
        for lens in await Lens.nodes.all()
        if (lens.provenance or "") == "system"
    }
    res = SeedResult(name="lenses")
    for key, spec in _LENSES.items():
        action = await upsert_lens(spec, key, mode, existing=by_key.get(key))
        tally(res, action)
    if res.created or res.updated:
        logger.info(
            f"Lenses: +{res.created} created, {res.updated} synced",
            extra={"tag": "lenses"},
        )
    return res
