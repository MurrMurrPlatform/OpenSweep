"""The escalation queue rotates instead of pinning to the newest 20.

Area runs are told not to investigate outside their lens — they file a stub
tagged `escalate:<global-lens-key>` and a global sweep picks it up. But the
digest sorted purely newest-first, truncated at _MAX_ESCALATIONS, and nothing
ever recorded that an item had been handed to a sweep. On a repo carrying
more than 20 open escalations the same newest 20 rode every sweep forever and
items 21+ were never seen at all — the cross-cutting channel silently dropped
its tail.

Delivery is now RANKED, not consumed, for the reason
`audit_selection.coverage_recency_for` spells out about coverage stamps: a
hard consumed-flag would drop an escalation permanently the first time a
sweep was handed it, including when that sweep then died.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.campaigns.services import part_dispatch

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _finding(uid, *, age_days=0, delivered_to=None, tags=("escalate:arch",), status="open"):
    return SimpleNamespace(
        uid=uid,
        title=f"finding {uid}",
        status=status,
        tags=list(tags),
        affected_paths=[f"src/{uid}.py"],
        created_at=NOW - timedelta(days=age_days),
        evidence=(
            {part_dispatch._DELIVERED_KEY: list(delivered_to)} if delivered_to else {}
        ),
    )


@pytest.fixture
def findings(monkeypatch):
    rows: list = []

    class _Nodes:
        @staticmethod
        async def filter(**_kw):
            return list(rows)

    import domains.findings.models as finding_models

    monkeypatch.setattr(finding_models.Finding, "nodes", _Nodes)
    return rows


async def _digest(lens="arch"):
    return await part_dispatch._escalation_digest("repo1", lens)


async def test_never_delivered_escalations_rank_before_delivered_ones(findings):
    findings.extend(
        [
            # Newest, but a sweep has already been handed it twice.
            _finding("seen", age_days=0, delivered_to=["r1", "r2"]),
            # Older, never delivered — this is the one starving today.
            _finding("fresh", age_days=30),
        ]
    )

    lines, rows = await _digest()

    assert [f.uid for f in rows] == ["fresh", "seen"]
    assert lines[0].startswith("- finding fresh")


async def test_among_delivered_escalations_the_least_delivered_goes_first(findings):
    findings.extend(
        [
            _finding("thrice", delivered_to=["a", "b", "c"]),
            _finding("once", delivered_to=["a"]),
            _finding("twice", delivered_to=["a", "b"]),
        ]
    )

    _lines, rows = await _digest()

    assert [f.uid for f in rows] == ["once", "twice", "thrice"]


async def test_the_digest_is_capped_and_reports_what_it_dropped(findings, caplog):
    findings.extend(_finding(f"f{i}", age_days=i) for i in range(30))

    lines, rows = await _digest()

    assert len(lines) == part_dispatch._MAX_ESCALATIONS == len(rows)
    assert "truncated" in caplog.text
    assert "20 of 30" in caplog.text


async def test_only_open_escalations_for_this_lens_are_carried(findings):
    findings.extend(
        [
            _finding("wrong-lens", tags=("escalate:other",)),
            _finding("closed", status="dismissed"),
            _finding("untagged", tags=()),
            _finding("keep"),
        ]
    )

    _lines, rows = await _digest()

    assert [f.uid for f in rows] == ["keep"]


async def test_marking_delivery_appends_the_run_and_is_idempotent():
    saved: list = []

    class _Row(SimpleNamespace):
        async def save(self):
            saved.append(self.uid)

    row = _Row(uid="f1", evidence={})
    await part_dispatch._mark_escalations_delivered([row], "run-A")
    await part_dispatch._mark_escalations_delivered([row], "run-A")
    await part_dispatch._mark_escalations_delivered([row], "run-B")

    assert row.evidence[part_dispatch._DELIVERED_KEY] == ["run-A", "run-B"]
    assert saved == ["f1", "f1"]  # the repeat delivery wrote nothing


async def test_a_failed_stamp_never_breaks_the_dispatch(caplog):
    class _Explodes(SimpleNamespace):
        async def save(self):
            raise RuntimeError("neo4j is having a moment")

    await part_dispatch._mark_escalations_delivered(
        [_Explodes(uid="f1", evidence={})], "run-A"
    )

    assert "could not stamp escalation delivery" in caplog.text
