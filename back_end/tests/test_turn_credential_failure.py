"""An undecryptable provider credential must fail the turn, not wedge the run.

`_run_turn_body` flips the run to RUNNING and saves BEFORE it builds the
subprocess turn — and building it unseals the provider credential via
`provider_secret()`, which raises `SecretBoxError` on a rotated/missing
OPENSWEEP_SECRETS_KEY or corrupted ciphertext. The surrounding handler only
catches GeneratorExit/CancelledError, so the exception escaped as an unhandled
500 and left the row stuck in RUNNING with no error recorded — an operator had
to unstick it in the database. It is now treated like the sibling spawn
failures: recorded as the turn's error so _finalize_turn lands the run in
awaiting_input with the reason on it.
"""

import pytest

from domains.runs.schemas import RunStatus
from domains.runs.services import turn_service as ts
from infrastructure.secretbox import SecretBoxError

pytestmark = pytest.mark.asyncio


class _Run:
    def __init__(self):
        self.uid = "run-cred"
        self.status = RunStatus.AWAITING_INPUT.value
        self.executor = "claude_code"
        self.turns = 0
        self.error = ""
        self.usage = {}
        self.updated_at = None
        self.last_activity_at = None

    async def save(self):
        return None


@pytest.fixture
def turn_env(monkeypatch):
    run = _Run()
    finalize_calls: list[str] = []

    async def _ensure_workspace(_run):
        return "/tmp/ws"

    async def _get_run(_self, _uid):
        return run

    async def _finalize(_self, uid, **kw):
        finalize_calls.append(kw["error_detail"])
        return False, RunStatus.AWAITING_INPUT.value, ""

    monkeypatch.setattr(ts.workspace_service, "ensure_workspace", _ensure_workspace)
    monkeypatch.setattr(ts.TurnService, "get_run", _get_run)
    monkeypatch.setattr(ts.TurnService, "_finalize_turn", _finalize)
    monkeypatch.setattr(ts, "append_event", lambda *a, **k: None)
    return run, finalize_calls


async def _drain(gen):
    return [ev async for ev in gen]


async def test_secretbox_error_fails_the_turn_instead_of_escaping(turn_env, monkeypatch):
    run, finalize_calls = turn_env

    async def _boom(_self, *_a, **_kw):
        raise SecretBoxError("OPENSWEEP_SECRETS_KEY cannot decrypt this value")

    monkeypatch.setattr(ts.TurnService, "_build_subprocess_turn", _boom)

    events = await _drain(ts.TurnService()._run_turn_body("run-cred", "hi", run, None))

    # The error surfaces on the stream instead of propagating as a 500...
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "credential could not be decrypted" in errors[0]["detail"]
    # ...and it is handed to the finalizer, which is what moves the run off
    # RUNNING and records the reason.
    assert len(finalize_calls) == 1
    assert "credential could not be decrypted" in finalize_calls[0]
    assert events[-1] == {"type": "status", "status": RunStatus.AWAITING_INPUT.value}
    # The turn reservation is released, so follow-ups don't 409 forever.
    assert "run-cred" not in ts._RUNNING


async def test_successful_build_still_spawns(turn_env, monkeypatch):
    run, finalize_calls = turn_env
    spawned: list[list[str]] = []

    async def _build(_self, *_a, **_kw):
        return ["claude", "-p"], {}

    async def _spawn(*argv, **_kw):
        spawned.append(list(argv))
        raise OSError("no binary here")  # stop before the streaming loop

    monkeypatch.setattr(ts.TurnService, "_build_subprocess_turn", _build)
    monkeypatch.setattr(ts.asyncio, "create_subprocess_exec", _spawn)

    events = await _drain(ts.TurnService()._run_turn_body("run-cred", "hi", run, None))

    assert spawned == [["claude", "-p"]]
    # The pre-existing spawn-failure path is untouched by the new branch.
    assert "failed to spawn claude" in finalize_calls[0]
    assert [e for e in events if e["type"] == "error"]
