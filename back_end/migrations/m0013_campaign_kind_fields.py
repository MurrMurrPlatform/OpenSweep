"""Backfill Campaign kind/selection/coverage_keys from legacy template/area_prefix.

Legacy campaigns stored their shape in `template` and `area_prefix`.  This
migration derives the new fields so all existing rows render correctly after
the schema upgrade.
"""

VERSION = 13
NAME = "campaign-kind-fields"

SCHEMA_UP = ["CREATE INDEX campaign_kind IF NOT EXISTS FOR (c:Campaign) ON (c.kind)"]
SCHEMA_DOWN = ["DROP INDEX campaign_kind IF EXISTS"]

UP = [
    "MATCH (c:Campaign) WHERE c.kind IS NULL "
    "SET c.kind = CASE c.template WHEN 'full' THEN 'batch' ELSE 'subsystem' END",
    "MATCH (c:Campaign) WHERE c.selection IS NULL "
    "SET c.selection = CASE c.template WHEN 'rotation' THEN 'rotation' ELSE 'all' END",
    "MATCH (c:Campaign) WHERE c.coverage_keys IS NULL "
    "SET c.coverage_keys = CASE WHEN c.area_prefix IS NULL OR c.area_prefix = '' "
    "THEN [] ELSE [c.area_prefix] END",
]
DOWN = []
