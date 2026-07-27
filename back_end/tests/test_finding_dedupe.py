"""DB-free tests: dedupe_key stability + collisions, title similarity, and
the create_finding merge/similarity-fallback behaviour (Finding faked)."""

import importlib
from datetime import datetime, timezone

import pytest

# The package's __init__ re-exports the create_finding *function* under the
# same name, which shadows the submodule on plain attribute imports.
create_finding_module = importlib.import_module(
    "domains.platform_tools.create_finding"
)

from domains.findings.schemas import normalize_tags
from domains.findings.services.dedupe import build_dedupe_key, titles_similar
from domains.platform_tools.create_finding import create_finding


def test_same_inputs_yield_same_key():
    a = build_dedupe_key(
        repository_uid="r1",
        title="SQL injection in /users", top_path="api/users.py",
    )
    b = build_dedupe_key(
        repository_uid="r1",
        title="SQL injection in /users", top_path="api/users.py",
    )
    assert a == b


def test_normalisation_drops_numbers_and_case():
    a = build_dedupe_key(
        repository_uid="r1",
        title="SQL injection at line 42 in /users", top_path="api/users.py",
    )
    b = build_dedupe_key(
        repository_uid="r1",
        title="sql injection at line 99 in users",
        top_path="api/users.py",
    )
    assert a == b


def test_different_repository_yields_different_key():
    a = build_dedupe_key(
        repository_uid="r1", title="t", top_path="p",
    )
    b = build_dedupe_key(
        repository_uid="r2", title="t", top_path="p",
    )
    assert a != b


def test_different_top_path_yields_different_key():
    a = build_dedupe_key(
        repository_uid="r1",
        title="t", top_path="api/users.py",
    )
    b = build_dedupe_key(
        repository_uid="r1",
        title="t", top_path="api/admin.py",
    )
    assert a != b


def test_missing_optional_fields_dont_crash():
    k = build_dedupe_key(
        repository_uid="", title="", top_path="",
    )
    assert isinstance(k, str) and len(k) == 24


def test_normalize_tags_dedupes_and_normalizes():
    assert normalize_tags(["Security", "security", " Flaky Test ", ""]) == [
        "security",
        "flaky-test",
    ]


def test_normalize_tags_handles_none():
    assert normalize_tags(None) == []


# ── titles_similar ───────────────────────────────────────────────────────────


def test_titles_similar_exact_and_rephrased():
    assert titles_similar("SQL injection in /users", "SQL injection in /users")
    assert titles_similar(
        "SQL injection in the /users endpoint",
        "Possible SQL injection in the users endpoint",
    )


def test_titles_similar_ignores_case_numbers_punctuation():
    assert titles_similar("Race condition at line 42!", "race condition at line 99")


def test_titles_similar_rejects_different_issues():
    assert not titles_similar(
        "SQL injection in /users", "Missing timeout on webhook delivery"
    )


def test_titles_similar_empty_titles_never_match():
    assert not titles_similar("", "")
    assert not titles_similar("something", "")


def test_titles_similar_threshold_is_tunable():
    a, b = "cache invalidation broken", "cache invalidation partly broken"
    assert titles_similar(a, b, threshold=0.75)
    assert not titles_similar(a, b, threshold=0.99)


# ── create_finding merge + similarity fallback (Finding faked, no DB) ────────


class _FakeNodes:
    def __init__(self, store: list):
        self._store = store

    async def get_or_none(self, **kwargs):
        for f in self._store:
            if all(getattr(f, k, None) == v for k, v in kwargs.items()):
                return f
        return None

    async def filter(self, **kwargs):
        # Supports neomodel's `field__in=[...]` alongside plain equality — the
        # similarity fallback matches several live statuses at once, and a
        # fake that only understood `==` would silently return nothing.
        def matches(f) -> bool:
            for key, expected in kwargs.items():
                if key.endswith("__in"):
                    if getattr(f, key[: -len("__in")], None) not in expected:
                        return False
                elif getattr(f, key, None) != expected:
                    return False
            return True

        return [f for f in self._store if matches(f)]


def _make_fake_finding_class(store: list):
    class FakeFinding:
        nodes = _FakeNodes(store)

        def __init__(self, **kwargs):
            defaults = dict(
                uid="",
                repository_uid="",
                tags=[],
                kind="defect",
                severity="medium",
                size="medium",
                subtype="",
                title="",
                confidence=0.7,
                description="",
                root_cause="",
                why_it_matters="",
                evidence={},
                suggested_fix="",
                affected_paths=[],
                dedupe_key="",
                detected_by_tool="",
                detected_by_rule="",
                source_run_uid=None,
                source_run_uids=[],
                last_confirmed_at=None,
                executor="manual",
                source_path="tool-call",
                parse_status="ok",
                status="open",
                created_at=None,
                updated_at=None,
            )
            defaults.update(kwargs)
            self.__dict__.update(defaults)

        async def save(self):
            if self not in store:
                store.append(self)

    return FakeFinding


async def _noop_write_audit(**kwargs):
    return None


@pytest.fixture
def finding_store(monkeypatch) -> list:
    store: list = []
    monkeypatch.setattr(
        create_finding_module, "Finding", _make_fake_finding_class(store)
    )
    monkeypatch.setattr(create_finding_module, "write_audit", _noop_write_audit)
    return store


def _seed_existing(store: list, **overrides):
    """Insert a pre-existing open finding the way create_finding would."""
    fields = dict(
        uid="existing-1",
        repository_uid="r1",
        kind="defect",
        title="SQL injection in /users",
        affected_paths=["api/users.py"],
        source_run_uid="run-1",
        source_run_uids=["run-1"],
        confidence=0.6,
        status="open",
    )
    fields.update(overrides)
    fields.setdefault(
        "dedupe_key",
        build_dedupe_key(
            repository_uid=fields["repository_uid"],
            title=fields["title"],
            top_path=(fields["affected_paths"] or [""])[0],
        ),
    )
    f = create_finding_module.Finding(**fields)
    store.append(f)
    return f


async def test_exact_hit_accumulates_run_uids_and_bumps_last_confirmed(finding_store):
    existing = _seed_existing(finding_store)
    before = datetime.now(timezone.utc)

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in /users",
        affected_paths=["api/users.py"],
        source_run_uid="run-2",
        confidence=0.9,
    )

    assert result["deduplicated"] is True
    assert result["finding_uid"] == existing.uid
    assert "confirmation recorded" in result["note"]
    assert existing.source_run_uids == ["run-1", "run-2"]
    assert existing.last_confirmed_at is not None
    assert existing.last_confirmed_at >= before
    assert existing.confidence == 0.9
    assert len(finding_store) == 1


async def test_exact_hit_backfills_run_uids_from_legacy_single_uid(finding_store):
    existing = _seed_existing(finding_store, source_run_uids=[])

    await create_finding(
        repository_uid="r1",
        title="SQL injection in /users",
        affected_paths=["api/users.py"],
        source_run_uid="run-2",
    )

    assert existing.source_run_uids == ["run-1", "run-2"]


async def test_exact_hit_same_run_not_duplicated_in_run_uids(finding_store):
    existing = _seed_existing(finding_store)

    await create_finding(
        repository_uid="r1",
        title="SQL injection in /users",
        affected_paths=["api/users.py"],
        source_run_uid="run-1",
    )

    assert existing.source_run_uids == ["run-1"]


async def test_similarity_hit_merges_instead_of_creating(finding_store):
    existing = _seed_existing(finding_store)

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        affected_paths=["api/users.py", "api/db.py"],
        source_run_uid="run-3",
    )

    assert result["deduplicated"] is True
    assert result["finding_uid"] == existing.uid
    assert result["dedupe_key"] == existing.dedupe_key
    assert "similar open finding matched" in result["note"]
    assert existing.source_run_uids == ["run-1", "run-3"]
    assert existing.last_confirmed_at is not None
    assert len(finding_store) == 1


async def test_similarity_hit_merges_into_an_already_ticketed_finding(finding_store):
    # Promotion is not a resolution: a re-found instance of a finding that
    # already has a ticket is the SAME issue and must merge into it. If the
    # similarity fallback only matched "open", promoting a finding would make
    # the next rephrased sighting fork a duplicate node.
    existing = _seed_existing(finding_store, status="ticketed", ticket_uid="t-1")

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        affected_paths=["api/users.py", "api/db.py"],
        source_run_uid="run-3",
    )

    assert result["deduplicated"] is True
    assert result["finding_uid"] == existing.uid


async def test_similarity_does_not_reopen_a_resolved_finding(finding_store):
    # A fixed finding is settled — a new sighting is a REGRESSION and deserves
    # its own node, not a silent merge into something marked done.
    _seed_existing(finding_store, status="fixed")

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        affected_paths=["api/users.py", "api/db.py"],
        source_run_uid="run-3",
    )

    assert result["deduplicated"] is False
    assert len(finding_store) == 2, "the regression gets its own node"


async def test_similarity_requires_same_kind(finding_store):
    _seed_existing(finding_store, kind="improvement")

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        kind="defect",
        affected_paths=["api/users.py"],
    )

    assert result["deduplicated"] is False
    assert len(finding_store) == 2


async def test_below_threshold_title_creates_new_finding(finding_store):
    _seed_existing(finding_store)

    result = await create_finding(
        repository_uid="r1",
        title="Missing rate limiting on login route",
        affected_paths=["api/users.py"],
        source_run_uid="run-3",
    )

    assert result["deduplicated"] is False
    assert len(finding_store) == 2


async def test_disjoint_affected_paths_never_similarity_match(finding_store):
    _seed_existing(finding_store)

    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        affected_paths=["api/admin.py"],
    )

    assert result["deduplicated"] is False
    assert len(finding_store) == 2


async def test_empty_affected_paths_never_similarity_match(finding_store):
    _seed_existing(finding_store)

    # No paths → different dedupe key (empty top_path) AND no overlap check
    # possible, so a new finding is created rather than similarity-merged.
    result = await create_finding(
        repository_uid="r1",
        title="SQL injection in users endpoint",
        affected_paths=[],
    )

    assert result["deduplicated"] is False
    assert len(finding_store) == 2


async def test_new_finding_records_confirmation_provenance(finding_store):
    result = await create_finding(
        repository_uid="r1",
        title="Missing timeout on webhook delivery",
        affected_paths=["hooks/deliver.py"],
        source_run_uid="run-9",
    )

    assert result["deduplicated"] is False
    (created,) = finding_store
    assert created.source_run_uids == ["run-9"]
    assert created.last_confirmed_at is not None


async def test_legacy_effort_alias_lands_in_size(finding_store):
    await create_finding(
        repository_uid="r1",
        title="Legacy effort alias",
        effort="small",
    )

    (created,) = finding_store
    assert created.size == "small"


async def test_explicit_size_wins_over_effort_alias(finding_store):
    await create_finding(
        repository_uid="r1",
        title="Size beats effort",
        size="large",
        effort="small",
    )

    (created,) = finding_store
    assert created.size == "large"
