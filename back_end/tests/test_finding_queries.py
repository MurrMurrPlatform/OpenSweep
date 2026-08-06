"""DB-free coverage for `domains.findings.queries.find_similar` — verifies
the query pushes its filters into Cypher instead of scanning the full
repository set. Regression pin for the N+1 fix in the api&findings roll-up
(OpenSweep-Ticket: 5082d197dc644e03b97227eac3d7ba5d)."""

from __future__ import annotations

import pytest

from domains.findings import queries as queries_module
from domains.findings.queries import _ACTIVE_STATUSES, find_similar


class _FakeNodeSet:
    """Neomodel `.nodes` stand-in that records every `.filter(**kw)` call."""

    def __init__(self, store: list[dict]) -> None:
        self._store = store
        self.calls: list[dict] = []

    async def filter(self, **kwargs):
        self.calls.append(kwargs)

        def matches(f: dict) -> bool:
            for key, expected in kwargs.items():
                if key.endswith("__in"):
                    if f.get(key[: -len("__in")]) not in expected:
                        return False
                elif key.endswith("__icontains"):
                    haystack = (f.get(key[: -len("__icontains")]) or "").lower()
                    if expected.lower() not in haystack:
                        return False
                elif f.get(key) != expected:
                    return False
            return True

        return [f for f in self._store if matches(f)]


@pytest.fixture
def fake_finding(monkeypatch):
    class FakeFinding:
        nodes = _FakeNodeSet(store=[])

    monkeypatch.setattr(queries_module, "Finding", FakeFinding)
    return FakeFinding


async def test_returns_empty_when_no_criteria(fake_finding):
    fake_finding.nodes._store.append({"dedupe_key": "abc", "status": "open"})
    result = await find_similar(repository_uid="r1")
    assert result == []
    assert fake_finding.nodes.calls == [], "no criteria → no Cypher hit"


async def test_dedupe_key_pushes_status_and_key_into_cypher(fake_finding):
    fake_finding.nodes._store.extend(
        [
            {"dedupe_key": "abc", "status": "open", "repository_uid": "r1", "title": "x"},
            {"dedupe_key": "abc", "status": "dismissed", "repository_uid": "r1", "title": "x"},
            {"dedupe_key": "other", "status": "open", "repository_uid": "r1", "title": "x"},
        ]
    )
    result = await find_similar(repository_uid="r1", dedupe_key="abc")
    assert [f["dedupe_key"] for f in result] == ["abc"]
    (call,) = fake_finding.nodes.calls
    assert call == {
        "repository_uid": "r1",
        "status__in": _ACTIVE_STATUSES,
        "dedupe_key": "abc",
    }


async def test_title_substring_uses_icontains(fake_finding):
    fake_finding.nodes._store.extend(
        [
            {"title": "SQL injection in /users", "status": "open", "repository_uid": "r1"},
            {"title": "unrelated bug", "status": "open", "repository_uid": "r1"},
            {"title": "sql", "status": "wont-fix", "repository_uid": "r1"},
        ]
    )
    result = await find_similar(repository_uid="r1", title_substring="SQL")
    assert [f["title"] for f in result] == ["SQL injection in /users"]
    (call,) = fake_finding.nodes.calls
    assert call == {
        "repository_uid": "r1",
        "status__in": _ACTIVE_STATUSES,
        "title__icontains": "SQL",
    }


async def test_dedupe_key_wins_over_title_substring(fake_finding):
    # Both provided → only the dedupe_key branch runs; title fallback would
    # otherwise return unrelated rows and the old code did just that.
    fake_finding.nodes._store.append(
        {"dedupe_key": "abc", "status": "open", "repository_uid": "r1", "title": "sql"}
    )
    result = await find_similar(
        repository_uid="r1", dedupe_key="abc", title_substring="ignored"
    )
    assert len(result) == 1
    (call,) = fake_finding.nodes.calls
    assert "dedupe_key" in call
    assert "title__icontains" not in call
