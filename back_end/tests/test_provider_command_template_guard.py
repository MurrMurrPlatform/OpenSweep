"""A custom cli_command_template is RCE, so only the instance operator may set
one — a tenant role="admin" (self-granted at org creation) must not.
"""

import pytest
from fastapi import HTTPException

from domains.llm_providers.services.llm_provider_service import (
    _guard_custom_command_template,
)
from domains.users.schemas import UserDTO


def _user(*, platform_admin: bool):
    return UserDTO(
        uid="u", email="e@x.y", display_name="U", role="admin",
        org_uid="org-a", org_role="owner", is_platform_admin=platform_admin,
    )


def test_tenant_admin_cannot_set_custom_template():
    with pytest.raises(HTTPException) as exc:
        _guard_custom_command_template("sh -c 'curl evil'", _user(platform_admin=False))
    assert exc.value.status_code == 403


def test_operator_can_set_custom_template():
    _guard_custom_command_template("sh -c 'anything'", _user(platform_admin=True))


def test_empty_template_is_always_allowed():
    # Empty means "use the kind's default" — the safe path every tenant takes.
    _guard_custom_command_template("", _user(platform_admin=False))
    _guard_custom_command_template("   ", _user(platform_admin=False))
    _guard_custom_command_template(None, _user(platform_admin=False))
