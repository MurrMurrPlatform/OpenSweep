"""Executor adapters.

PLATFORM.md §Executors: the platform itself does not do agentic code
work. It dispatches Runs to one of:

- claude_code    (Phase 5)
- opencode       (tracking CLI adapter; write-capable since G1)
- manual         (Phase 4 — humans posting results via the HTTP transport)
"""
