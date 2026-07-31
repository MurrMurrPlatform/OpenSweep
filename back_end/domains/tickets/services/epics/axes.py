"""HOW the selected tickets are cut into epics — one function per axis. Pure.

Step two of the contract in `schemas`. Six axes are arithmetic: an area key
matches or it does not, two tickets' paths intersect or they do not. This
module is the whole of that arithmetic, which is why grouping is no longer a
thing an agent eyeballs — the axes in `AGENT_AXES` are the judgment left over,
and they are raised, not faked.

Two invariants hold for EVERY axis:

* **A ticket lands in at most one epic.** Overlapping epics would dispatch
  two runs that edit the same ticket, so where a ticket has several candidate
  keys (three areas, four tags) it is assigned exactly one — the most
  specific area, the strongest class — rather than duplicated.
* **Nothing is silently lost.** Groups below `min_members` are dropped
  (an epic of one is just a ticket), oversized groups are SPLIT rather than
  truncated, and when `max_epics` bites, the survivors say so: every draft
  carries `evidence["capped_from"]`, the number of epics that existed
  before the cap. A capped list with no such marker reads as "that's all
  there was", which is how a rule quietly stops covering half its backlog.

Drafts come back STRONGEST FIRST: more members, then higher aggregate member
priority, then title — so the cap keeps the epices worth running, and the
ordering is itself the ranking the caller renders.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from math import ceil

from domains.tickets.services.epics.schemas import (
    MAX_RATIONALE_CHARS,
    EpicAxis,
    EpicDraft,
    EpicPartition,
    TicketFacts,
)
from infrastructure.ranking import PRIORITY_ORDER, priority_rank

#: How many shared tokens (paths, finding uids) an evidence payload carries.
#: The review card renders ONE LINE from this; the exact count travels
#: alongside, so "+7 more" stays renderable without shipping seven strings.
_EVIDENCE_SAMPLE = 3


@dataclass(frozen=True)
class _Group:
    """A candidate epic before sizing: members plus the fact that binds them."""

    #: Stable, human-meaningful identity — the area key, the lens, the shared
    #: path. Doubles as the deterministic sort key between equal-strength
    #: groups and as the head of the draft title.
    label: str
    members: list[TicketFacts]
    evidence: dict = field(default_factory=dict)


# ── axis groupers ──────────────────────────────────────────────────────────


def _most_specific(keys: Iterable[str]) -> str:
    """The deepest key, ties broken lexicographically.

    A ticket whose paths fall under both "back_end" and
    "back_end/domains/areas" belongs to the second: the deeper key is the one
    that actually describes the work, and picking exactly one is what keeps
    the ticket out of two epics.
    """
    real = [k for k in keys if k]
    if not real:
        return ""
    return min(real, key=lambda k: (-k.count("/"), k))


def _group_by_key(
    facts: list[TicketFacts],
    key_of: Callable[[TicketFacts], str],
    *,
    evidence_field: str,
    axis: str,
) -> list[_Group]:
    """Bucket tickets by a single-valued key, dropping the keyless."""
    buckets: dict[str, list[TicketFacts]] = {}
    for f in facts:
        key = key_of(f)
        if key:
            buckets.setdefault(key, []).append(f)
    return [
        _Group(
            label=key,
            members=members,
            evidence={
                evidence_field: key,
                "axis": axis,
                "member_count": len(members),
            },
        )
        for key, members in buckets.items()
    ]


def by_area(facts: list[TicketFacts]) -> list[_Group]:
    """Group on the subsystem area a ticket's paths fall under."""
    return _group_by_key(
        facts,
        lambda f: _most_specific(f.area_keys),
        evidence_field="area_key",
        axis="area",
    )


def by_feature(facts: list[TicketFacts]) -> list[_Group]:
    """Group on the feature area a ticket's paths fall under. The feature axis
    tiles the tree independently of subsystems, so a ticket can hold keys on
    both — same most-specific rule, applied per axis."""
    return _group_by_key(
        facts,
        lambda f: _most_specific(f.feature_keys),
        evidence_field="feature_key",
        axis="feature",
    )


def by_lens(facts: list[TicketFacts]) -> list[_Group]:
    """Group on finding kind — "all the security tickets".

    This is the one axis whose members are NOT expected to touch the same
    files: it spreads across the whole repository. Worth knowing when sizing
    `max_members`, since those members have the least to gain from sharing a
    single pull request."""
    return _group_by_key(facts, lambda f: f.kind, evidence_field="lens", axis="lens")


def by_class(facts: list[TicketFacts]) -> list[_Group]:
    """Group on the finding class — the `tag/subtype` pair.

    Two tickets sharing a class describe the same KIND of problem, so one epic
    can carry a single structural fix for all of them rather than n scattered
    patches.

    A finding with several tags belongs to several classes, but an epic member
    can only sit in one group. Each ticket is therefore assigned to its
    STRONGEST candidate class — the one with the most instances in this pool,
    ties broken lexicographically — since that is the class whose shared fix
    pays for itself most.
    """
    candidates = {
        f.uid: [f"{tag}/{f.subtype}" for tag in f.tags if tag] if f.subtype else []
        for f in facts
    }
    instances: dict[str, int] = {}
    for keys in candidates.values():
        for key in keys:
            instances[key] = instances.get(key, 0) + 1

    def _strongest(f: TicketFacts) -> str:
        keys = candidates[f.uid]
        if not keys:
            return ""
        return min(keys, key=lambda k: (-instances[k], k))

    return _group_by_key(facts, _strongest, evidence_field="class", axis="class")


def _components(
    facts: list[TicketFacts], tokens_of: Callable[[TicketFacts], Iterable[str]]
) -> list[tuple[list[TicketFacts], list[str]]]:
    """Connected components over "shares a token with", plus each component's
    shared tokens.

    TRANSITIVE by construction: A and B share `service.py`, B and C share
    `client.py`, so all three edit code that B touches and belong in one PR.
    Pairwise grouping would have split them and let two runs collide on B.

    Union-find over ticket indices: every claimant of a token is unioned with
    that token's first claimant, which is what makes the relation transitive
    without ever materializing the pair graph.
    """
    parent = list(range(len(facts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # dict.fromkeys dedupes within one ticket — a path listed twice is one claim.
    claimants: dict[str, list[int]] = {}
    for i, f in enumerate(facts):
        for token in dict.fromkeys(tokens_of(f)):
            if token:
                claimants.setdefault(token, []).append(i)
    for idxs in claimants.values():
        for other in idxs[1:]:
            union(idxs[0], other)

    members: dict[int, list[TicketFacts]] = {}
    for i, f in enumerate(facts):
        members.setdefault(find(i), []).append(f)

    # A component's evidence is the tokens more than one ticket claims — the
    # overlap that put them together, not every path they touch. All claimants
    # of a token share a root, so the token belongs to exactly one component.
    shared: dict[int, list[str]] = {}
    for token, idxs in claimants.items():
        if len(idxs) > 1:
            shared.setdefault(find(idxs[0]), []).append(token)

    return [(group, sorted(shared.get(root, []))) for root, group in members.items()]


def by_files(facts: list[TicketFacts]) -> list[_Group]:
    """Group on overlapping `paths`, transitively (see `_components`)."""
    groups: list[_Group] = []
    for members, tokens in _components(facts, lambda f: f.paths):
        if not tokens:  # a lone ticket sharing nothing — no epic to make
            continue
        groups.append(
            _Group(
                label=tokens[0],
                members=members,
                evidence={
                    "shared_paths": tokens[:_EVIDENCE_SAMPLE],
                    "shared_path_count": len(tokens),
                    "axis": "files",
                    "member_count": len(members),
                },
            )
        )
    return groups


def by_linked(facts: list[TicketFacts]) -> list[_Group]:
    """Group on shared `finding_uids`, transitively — tickets promoted from
    the same finding, or from findings that cite each other."""
    groups: list[_Group] = []
    for members, tokens in _components(facts, lambda f: f.finding_uids):
        if not tokens:
            continue
        groups.append(
            _Group(
                label=tokens[0],
                members=members,
                evidence={
                    "shared_finding_uids": tokens[:_EVIDENCE_SAMPLE],
                    "shared_finding_count": len(tokens),
                    "axis": "linked",
                    "member_count": len(members),
                },
            )
        )
    return groups


_GROUPERS: dict[EpicAxis, Callable[[list[TicketFacts]], list[_Group]]] = {
    EpicAxis.AREA: by_area,
    EpicAxis.FEATURE: by_feature,
    EpicAxis.FILES: by_files,
    EpicAxis.LENS: by_lens,
    EpicAxis.CLASS: by_class,
    EpicAxis.LINKED: by_linked,
}


# ── sizing, ranking, drafting ──────────────────────────────────────────────


def _chunks(members: list[TicketFacts], max_members: int) -> list[list[TicketFacts]]:
    """Split an oversized group into the fewest NEAR-EQUAL chunks.

    Slicing by `max_members` leaves an orphan tail (7 members at max 6 → 6+1)
    that then fails `min_members` and costs a whole ticket; 4+3 keeps both
    halves reviewable. Members keep their selection order, so the
    highest-priority tickets stay in the first chunk.
    """
    if max_members <= 0 or len(members) <= max_members:
        return [members]
    count = ceil(len(members) / max_members)
    base, extra = divmod(len(members), count)
    out: list[list[TicketFacts]] = []
    start = 0
    for i in range(count):
        size = base + (1 if i < extra else 0)
        out.append(members[start : start + size])
        start += size
    return out


def _suggested_priority(members: list[TicketFacts]) -> str:
    """The epic inherits its most urgent member: one PR closing an urgent
    ticket cannot itself be scheduled as low."""
    top = max(priority_rank(m.priority) for m in members)
    return next(name for name, rank in PRIORITY_ORDER.items() if rank == top)


def _shared_labels(members: list[TicketFacts]) -> list[str]:
    """Labels EVERY member carries. The union would drown the epic in the
    labels of whichever member had the most; the intersection only keeps what
    is true of the epic as a whole."""
    common = set(members[0].labels)
    for m in members[1:]:
        common &= set(m.labels)
    return sorted(common)


def partition(facts: list[TicketFacts], spec: EpicPartition) -> list[EpicDraft]:
    """Cut the selected tickets into `EpicDraft`s along `spec.axis`.

    Groups → drop below `min_members` → split above `max_members` into
    near-equal chunks → rank → cap at `max_epics`.

    Ordering IS the ranking: strongest first, where strength is (member count,
    aggregate member priority, title). The cap therefore keeps the epices
    with the most work and the most urgency in them.

    When the cap drops epics, every surviving draft carries
    `evidence["capped_from"] = <candidate epic count>`. A silently truncated
    list looks identical to a small backlog, and a rule that quietly stopped
    covering half its tickets is the failure mode this whole module exists to
    remove. Chunks of a split group also carry `evidence["part"]` /
    `evidence["of_parts"]` so an epic that is one third of an area does not
    read as the whole of it.

    Raises `ValueError` for `MANUAL` (a human ticked the boxes; there is
    nothing to compute) and for every `AGENT_AXES` member (they need the
    grouping agent). Returning `[]` for those would look like "no epics found".
    """
    grouper = _GROUPERS.get(spec.axis)
    if grouper is None:
        raise ValueError(
            f"axis {spec.axis.value!r} is not deterministic and cannot be "
            f"partitioned here; computable axes are "
            f"{sorted(a.value for a in _GROUPERS)}"
        )

    groups = [g for g in grouper(facts) if len(g.members) >= max(spec.min_members, 1)]

    sized: list[tuple[_Group, list[TicketFacts], int, int]] = []
    for group in groups:
        chunks = _chunks(group.members, spec.max_members)
        for part, chunk in enumerate(chunks, start=1):
            # A split can strand a remainder below min_members; it is a
            # ticket, not an epic, so it drops here too.
            if len(chunk) >= max(spec.min_members, 1):
                sized.append((group, chunk, part, len(chunks)))

    # Strongest first. Label + part number close the ordering so that two
    # equally strong epics never swap places between runs.
    sized.sort(
        key=lambda s: (
            -len(s[1]),
            -sum(priority_rank(m.priority) for m in s[1]),
            s[0].label,
            s[2],
        )
    )

    kept = sized[: spec.max_epics] if spec.max_epics > 0 else sized
    capped_from = len(sized) if len(kept) < len(sized) else 0
    return [
        _build_draft(group, chunk, part, of_parts, spec.axis, capped_from)
        for group, chunk, part, of_parts in kept
    ]


def _build_draft(
    group: _Group,
    chunk: list[TicketFacts],
    part: int,
    of_parts: int,
    axis: EpicAxis,
    capped_from: int,
) -> EpicDraft:
    """One sized chunk → one draft, with the evidence the review card renders.

    `evidence` leads with the identifying fact (the area key, the first shared
    path) because the UI renders one line from it and truncates the rest.
    """
    evidence = dict(group.evidence)
    evidence["member_count"] = len(chunk)
    if of_parts > 1:
        evidence["part"] = part
        evidence["of_parts"] = of_parts
    if capped_from:
        evidence["capped_from"] = capped_from

    tickets = f"{len(chunk)} ticket{'' if len(chunk) == 1 else 's'}"
    title = f"{group.label} · {tickets}"
    if of_parts > 1:
        title = f"{title} ({part}/{of_parts})"

    return EpicDraft(
        title=title,
        member_ticket_uids=[m.uid for m in chunk],
        axis=axis,
        evidence=evidence,
        rationale=_rationale(axis, group.label, len(chunk)),
        suggested_priority=_suggested_priority(chunk),
        suggested_labels=_shared_labels(chunk),
    )


#: One sentence per axis, naming WHY these tickets are one epic. Bounded by
#: `MAX_RATIONALE_CHARS` — the unbounded agent string rendered as markdown is
#: where the unreadable review card started.
_RATIONALE = {
    EpicAxis.AREA: "{n} tickets in area {label} — one area, one PR.",
    EpicAxis.FEATURE: "{n} tickets in feature {label} — one feature, one PR.",
    EpicAxis.FILES: "{n} tickets whose changes overlap on {label} and would collide on separate branches.",
    EpicAxis.LENS: "{n} {label} tickets — same lens, no expected file overlap.",
    EpicAxis.CLASS: "{n} tickets of finding class {label} — one guard covers the class.",
    EpicAxis.LINKED: "{n} tickets linked through finding {label}.",
}


def _rationale(axis: EpicAxis, label: str, count: int) -> str:
    text = _RATIONALE[axis].format(n=count, label=label)
    return text[:MAX_RATIONALE_CHARS]
