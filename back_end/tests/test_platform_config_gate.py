"""F7 (LOW) — platform-config read gate + dev-reset import.

WHY:
  - `GET /api/v1/platform-config` had NO route dependency, so any authenticated
    user of any org could read the platform kill-switch state — its POST
    sibling is correctly `require_platform_admin`.
  - `POST /api/v1/platform-config/dev-reset` referenced `settings` which was
    never imported, so its "dev/test environments only" guard raised NameError
    before it could run (fails closed today, but the guard was dead code).

WHAT: `get_config` depends on `require_platform_admin`, and the module imports
`settings` so the dev-reset environment guard actually executes.
"""

import inspect

from fastapi.params import Depends as DependsParam

import api.v1.platform_config as pc
from api.dependencies import require_platform_admin


def _depends(fn):
    return [p.default for p in inspect.signature(fn).parameters.values()
            if isinstance(p.default, DependsParam)]


def test_get_config_requires_platform_admin():
    assert any(d.dependency is require_platform_admin for d in _depends(pc.get_config))


def test_settings_imported_for_dev_reset_guard():
    # Without the import the env guard raised NameError instead of gating.
    assert getattr(pc, "settings", None) is not None
