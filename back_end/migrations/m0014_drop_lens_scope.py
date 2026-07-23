VERSION = 14
NAME = "drop-lens-scope"
SCHEMA_UP: list[str] = []
SCHEMA_DOWN: list[str] = []
UP: list[str] = ["MATCH (l:Lens) REMOVE l.scope"]
DOWN: list[str] = []  # value drop; re-seed restores defaults
