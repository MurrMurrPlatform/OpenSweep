"""Migrations must not backfill a JSONProperty with a native Cypher literal.

WHY: neomodel's `JSONProperty` stores a JSON *string* (`deflate({}) == '{}'`).
A migration that writes a bare Cypher map — `SET p.evidence = {}` — does not
merely store the wrong shape: Neo4j rejects map-valued properties outright
("Property values can only be of primitive types or arrays thereof.
Encountered: Map{}"), the migration raises, and `migration_runner` takes the
whole backend down on boot. A bare `[]` is worse in a quieter way: Neo4j
accepts it as a native array, so boot succeeds and the breakage surfaces later
as an inflate error on read.

This bit m0017 and m0018, which were the first migrations in the repo to
backfill a JSONProperty — there was no precedent to copy. This test is the
precedent.
"""

from __future__ import annotations

import pkgutil
import re

import migrations

#: `SET <alias>.<prop> = {` or `= [` — a native map/array literal, as opposed
#: to the quoted '{}' / '[]' a JSONProperty needs.
_NATIVE_LITERAL = re.compile(r"SET\s+\w+\.(\w+)\s*=\s*[\{\[]")


def _statements(module) -> list[str]:
    out: list[str] = []
    for attr in ("UP", "DOWN", "SCHEMA_UP", "SCHEMA_DOWN"):
        out.extend(str(s) for s in getattr(module, attr, []) or [])
    return out


def _json_property_names() -> set[str]:
    """Every property name declared as a JSONProperty anywhere in the graph."""
    from neomodel import AsyncStructuredNode
    from neomodel.properties import JSONProperty

    import domains.tickets.models  # noqa: F401 — registers the node classes

    names: set[str] = set()
    stack = [AsyncStructuredNode]
    seen = set()
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        for name, prop in vars(cls).items():
            if isinstance(prop, JSONProperty):
                names.add(name)
    return names


def test_no_migration_backfills_a_json_property_with_a_native_literal():
    json_props = _json_property_names()
    assert json_props, "found no JSONProperty at all — the scan is broken"

    offenders: list[str] = []
    for info in pkgutil.iter_modules(migrations.__path__):
        if not info.name.startswith("m0"):
            continue
        module = __import__(f"migrations.{info.name}", fromlist=["_"])
        for stmt in _statements(module):
            for prop in _NATIVE_LITERAL.findall(stmt):
                if prop in json_props:
                    offenders.append(f"{info.name}: {prop} in {stmt!r}")

    assert not offenders, (
        "JSONProperty backfilled with a native Cypher literal — quote it "
        "('{}' / '[]'):\n  " + "\n  ".join(offenders)
    )
