"""Cypher label interpolation in _derive_repository_uid (infrastructure/audit.py).

WHY: `_derive_repository_uid` builds `f"MATCH (n:{subject_type} ...)"` — labels
can't be parameterized in Cypher, so subject_type is interpolated straight into
the query text. A regex guard alone is a maintenance burden (anything the
regex misses is a live injection); a closed whitelist of the exact labels
write_audit() call sites use is the actual guard. DB-free: proves rejected
subject_types never reach cypher_query at all.
"""

import pytest

from infrastructure.audit import _ALLOWED_LABELS, _derive_repository_uid

pytestmark = pytest.mark.asyncio


async def test_known_label_is_allowed():
    assert "Finding" in _ALLOWED_LABELS
    assert "Ticket" in _ALLOWED_LABELS


async def test_repository_subject_type_short_circuits_without_a_query(monkeypatch):
    calls = []

    async def spy(*a, **kw):
        calls.append((a, kw))
        return [["should-not-be-used"]], None

    monkeypatch.setattr("neomodel.adb.cypher_query", spy)
    assert await _derive_repository_uid("repo-1", "Repository") == "repo-1"
    assert calls == []


async def test_unknown_label_returns_empty_and_never_queries(monkeypatch):
    calls = []

    async def spy(*a, **kw):
        calls.append((a, kw))
        return [["leaked"]], None

    monkeypatch.setattr("neomodel.adb.cypher_query", spy)
    assert await _derive_repository_uid("u1", "NotARealLabel") == ""
    assert calls == []  # a swallowed exception must not be mistaken for "blocked"


@pytest.mark.parametrize(
    "payload",
    [
        "Finding) DETACH DELETE n //",
        "Finding {uid: $uid}) DETACH DELETE (m) //",
        "Finding` OR 1=1 //",
        "Finding; MATCH (a) DETACH DELETE a",
        "finding",  # whitelist is case-sensitive — lowercase must not pass
        "",
        "  ",
    ],
)
async def test_injection_payloads_are_rejected_without_a_query(monkeypatch, payload):
    calls = []

    async def spy(*a, **kw):
        calls.append((a, kw))
        return [["leaked"]], None

    monkeypatch.setattr("neomodel.adb.cypher_query", spy)
    assert await _derive_repository_uid("u1", payload) == ""
    assert calls == []  # a swallowed exception must not be mistaken for "blocked"


async def test_valid_label_queries_with_the_label_interpolated(monkeypatch):
    calls = []

    async def fake_cypher_query(query, params):
        calls.append((query, params))
        return [["repo-9"]], None

    monkeypatch.setattr("neomodel.adb.cypher_query", fake_cypher_query)
    result = await _derive_repository_uid("u1", "Finding")
    assert result == "repo-9"
    assert calls == [("MATCH (n:Finding {uid: $uid}) RETURN n.repository_uid", {"uid": "u1"})]
