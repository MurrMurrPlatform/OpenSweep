"""ask_user tool: registration + validation surface (pure)."""

import pytest
from fastapi import HTTPException

from domains.platform_tools.ask_user import _validate
from domains.platform_tools.dispatcher import _TOOLS


def test_tool_is_registered_in_dispatcher():
    assert "ask_user" in _TOOLS


def test_validation_rejects_empty_question():
    with pytest.raises(HTTPException) as exc:
        _validate(thread_uid="th-1", question="  ", options=[])
    assert exc.value.status_code == 422


def test_validation_rejects_missing_thread():
    with pytest.raises(HTTPException):
        _validate(thread_uid="", question="Which auth flow?", options=[])


def test_validation_caps_options():
    with pytest.raises(HTTPException):
        _validate(thread_uid="th-1", question="q", options=[str(i) for i in range(7)])
    _validate(thread_uid="th-1", question="q", options=["a", "b"])  # ok
