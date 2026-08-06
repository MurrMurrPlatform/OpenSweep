"""read_findings: the `status` filter's "everything" sentinel.

`status` goes into a plain equality filter, so any value that isn't a real
Finding status matches nothing. Agents reach for "all" — and the tool's own
`status="open"` default makes "open" look like one option among several — so
an attempt to list EVERY finding silently returned zero rather than
everything. A wrong answer that looks like a legitimate empty result is worse
than an error, so the obvious synonyms are now spelled explicitly.
"""

import pytest

from domains.platform_tools.read_findings import _ANY_STATUS, _status_filter


@pytest.mark.parametrize("sentinel", ["all", "any", "*", "", "  ALL  "])
def test_sentinels_mean_no_status_filter(sentinel):
    assert _status_filter(sentinel) is None


def test_none_means_no_status_filter():
    assert _status_filter(None) is None


@pytest.mark.parametrize("status", ["open", "ticketed", "resolved", "dismissed"])
def test_real_statuses_still_filter(status):
    assert _status_filter(status) == status


def test_status_is_normalized_before_filtering():
    # Agents emit "Open"/" open "; the stored property is lowercase.
    assert _status_filter("  Open ") == "open"


def test_empty_string_remains_the_documented_escape_hatch():
    # The pre-existing no-filter spelling must keep working — callers rely on it.
    assert "" in _ANY_STATUS
