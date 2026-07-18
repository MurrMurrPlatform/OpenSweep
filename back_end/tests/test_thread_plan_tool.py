"""submit_thread_plan tool: registration + validation surface (pure)."""

import pytest
from fastapi import HTTPException

from domains.platform_tools.dispatcher import _TOOLS
from domains.platform_tools.submit_thread_plan import _validate


def test_tool_is_registered_in_dispatcher():
    assert "submit_thread_plan" in _TOOLS


def test_validation_rejects_empty_plan():
    with pytest.raises(HTTPException) as exc:
        _validate(thread_uid="th-1", plan_markdown="   ")
    assert exc.value.status_code == 422


def test_validation_rejects_missing_thread_uid():
    with pytest.raises(HTTPException):
        _validate(thread_uid="", plan_markdown="## Plan")
