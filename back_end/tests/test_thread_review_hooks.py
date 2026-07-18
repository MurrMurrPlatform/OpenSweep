"""Review-loop thread hooks: no-ops without a PR uid, never raise (Phase 3)."""

import asyncio
from types import SimpleNamespace

import domains.threads.services.hooks as hooks


def test_verdict_hook_noop_without_pr_uid():
    asyncio.run(hooks.note_verdict_for_pr("", result="approve", verdict_uid="v", sha="s"))


def test_fix_hook_noop_without_pr_uid():
    asyncio.run(hooks.note_fix_run_for_pr("", SimpleNamespace(uid="r-1")))


def test_verdict_hook_never_raises(monkeypatch):
    class Boom:
        def filter(self, **kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        "domains.threads.models.Thread", SimpleNamespace(nodes=Boom()), raising=False
    )
    asyncio.run(hooks.note_verdict_for_pr("pr-1", result="approve", verdict_uid="v", sha="s"))


def test_fix_hook_never_raises(monkeypatch):
    class Boom:
        def filter(self, **kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        "domains.threads.models.Thread", SimpleNamespace(nodes=Boom()), raising=False
    )
    asyncio.run(hooks.note_fix_run_for_pr("pr-1", SimpleNamespace(uid="r-1")))
