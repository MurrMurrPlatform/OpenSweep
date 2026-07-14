"""Per-org LLM providers — strictly org-owned: visibility, per-scope active,
management gates. There is NO shared/platform scope; org_uid == "" rows are
legacy-unowned data (invisible, unselectable, unmanageable).
DB-free: LLMProvider is monkeypatched with an in-memory fake."""

import pytest
from fastapi import HTTPException

import domains.llm_providers.services.llm_provider_service as svc_mod
from domains.llm_providers.schemas import (
    KIND_CATALOG,
    CreateLLMProviderRequest,
    LLMProviderKind,
    UpdateLLMProviderRequest,
    default_cli_template,
)
from domains.llm_providers.services.llm_provider_service import (
    LLMProviderService,
    choose_provider,
)
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


class _Node:
    def __init__(self, **kw):
        defaults = dict(
            org_uid="", label="p", kind="mlx", base_url="", model="", api_key_env="",
            cli_command_template="", extra_args="", enabled=True, active=False,
            fallback_priority=100, notes="", credential_secret="",
            last_health_check_at=None, last_health_status="unknown",
            last_health_detail="", created_at=None, updated_at=None,
        )
        defaults.update(kw)
        self.__dict__.update(defaults)

    async def save(self):
        if self not in _STORE:
            _STORE.append(self)
        return self

    async def delete(self):
        _STORE.remove(self)


_STORE: list[_Node] = []


class _Nodes:
    async def all(self):
        return list(_STORE)

    async def get_or_none(self, **kw):
        for n in _STORE:
            if all(getattr(n, k, None) == v for k, v in kw.items()):
                return n
        return None


class FakeLLMProvider(_Node):
    nodes = _Nodes()


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    _STORE.clear()
    monkeypatch.setattr(svc_mod, "LLMProvider", FakeLLMProvider)

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(svc_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _user(org="org-a", platform=False, role="admin"):
    return UserDTO(
        uid="u1", email="a@b.c", display_name="A", role=role,
        org_uid=org, org_role="owner", is_platform_admin=platform,
    )


def _seed(**kw):
    n = FakeLLMProvider(uid=kw.pop("uid"), **kw)
    _STORE.append(n)
    return n


# ── Visibility ───────────────────────────────────────────────────────────────


async def test_list_shows_own_org_only():
    _seed(uid="unowned-1", org_uid="")  # legacy-unowned: invisible to everyone
    _seed(uid="mine-1", org_uid="org-a")
    _seed(uid="theirs-1", org_uid="org-b")
    listed = await LLMProviderService().list_providers("org-a")
    assert {p.uid for p in listed} == {"mine-1"}


async def test_orgless_caller_sees_nothing():
    _seed(uid="unowned-1", org_uid="")
    _seed(uid="mine-1", org_uid="org-a")
    assert await svc_mod.visible_providers("") == []
    assert await svc_mod.get_active_provider("") is None


async def test_get_cross_org_provider_404s():
    _seed(uid="theirs-1", org_uid="org-b")
    with pytest.raises(HTTPException) as exc:
        await LLMProviderService().get("theirs-1", "org-a")
    assert exc.value.status_code == 404


# ── Selection (runs) ─────────────────────────────────────────────────────────


async def test_no_fallback_to_unowned_providers():
    _seed(uid="unowned-1", org_uid="", active=True)
    _seed(uid="mine-1", org_uid="org-a", active=True)
    picked = await svc_mod.select_provider(org_uid="org-a")
    assert picked.uid == "mine-1"
    # another org (no providers of its own) gets NOTHING — no shared fallback
    assert await svc_mod.select_provider(org_uid="org-b") is None


async def test_selection_never_uses_other_orgs_provider():
    _seed(uid="theirs-1", org_uid="org-b", active=True)
    assert await svc_mod.select_provider(org_uid="org-a") is None
    assert await svc_mod.get_active_provider("org-a") is None


async def test_fallback_chain_stays_inside_the_org():
    _seed(uid="mine-1", org_uid="org-a", active=True, last_health_status="unreachable")
    _seed(uid="mine-2", org_uid="org-a", fallback_priority=50)
    _seed(uid="theirs-1", org_uid="org-b", fallback_priority=1)  # best priority, wrong org
    picked = await svc_mod.select_provider(org_uid="org-a")
    assert picked.uid == "mine-2"


# ── Management gates ─────────────────────────────────────────────────────────


async def test_org_admin_creates_org_scoped_provider_with_own_token():
    req = CreateLLMProviderRequest(
        label="Acme Claude", kind=LLMProviderKind.CLAUDE_API, credential_secret="sk-acme"
    )
    dto = await LLMProviderService().create(req, user=_user("org-a"))
    assert dto.org_uid == "org-a"
    assert dto.has_credential_secret is True  # secret itself never returned


async def test_create_always_stamps_the_callers_org_even_for_platform_admins():
    req = CreateLLMProviderRequest(label="Admin's", kind=LLMProviderKind.MLX)
    dto = await LLMProviderService().create(req, user=_user("org-a", platform=True))
    assert dto.org_uid == "org-a"  # no shared=True path exists anymore


async def test_create_without_an_org_is_422():
    req = CreateLLMProviderRequest(label="Nowhere", kind=LLMProviderKind.MLX)
    with pytest.raises(HTTPException) as exc:
        await LLMProviderService().create(req, user=_user(""))
    assert exc.value.status_code == 422


async def test_unowned_provider_is_unmanageable():
    _seed(uid="unowned-1", org_uid="")
    with pytest.raises(HTTPException) as exc:
        await LLMProviderService().update(
            "unowned-1", UpdateLLMProviderRequest(label="hijack"), user=_user("org-a")
        )
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await LLMProviderService().delete("unowned-1", user=_user("org-a"))
    assert exc.value.status_code == 404


async def test_editing_another_orgs_provider_404s():
    _seed(uid="theirs-1", org_uid="org-b")
    with pytest.raises(HTTPException) as exc:
        await LLMProviderService().update(
            "theirs-1", UpdateLLMProviderRequest(label="x"), user=_user("org-a")
        )
    assert exc.value.status_code == 404  # existence never leaks


async def test_activation_is_per_scope():
    theirs = _seed(uid="theirs-1", org_uid="org-b", active=True)
    _seed(uid="mine-1", org_uid="org-a", active=False)
    await LLMProviderService().update(
        "mine-1", UpdateLLMProviderRequest(active=True), user=_user("org-a")
    )
    # activating an org-a provider must not deactivate org-b's
    assert theirs.active is True
    mine = await FakeLLMProvider.nodes.get_or_none(uid="mine-1")
    assert mine.active is True


# ── CLI template defaulting (platform-owned) ─────────────────────────────────


def test_default_cli_template_lookup():
    assert "claude -p" in default_cli_template(LLMProviderKind.CLAUDE_SUBSCRIPTION)
    assert default_cli_template("claude_api") == ""       # HTTP kind — no CLI
    assert default_cli_template("no-such-kind") == ""


async def test_create_defaults_the_cli_template_for_cli_kinds():
    # The UI can submit an empty template (prefill only fires on kind change);
    # the row must still be dispatchable by the claude_code adapter.
    req = CreateLLMProviderRequest(label="Claude", kind=LLMProviderKind.CLAUDE_SUBSCRIPTION)
    dto = await LLMProviderService().create(req, user=_user("org-a"))
    assert dto.cli_command_template == default_cli_template(LLMProviderKind.CLAUDE_SUBSCRIPTION)
    assert "{{instruction_q}}" in dto.cli_command_template


async def test_create_keeps_an_explicit_cli_template():
    req = CreateLLMProviderRequest(
        label="Claude", kind=LLMProviderKind.CLAUDE_SUBSCRIPTION,
        cli_command_template="claude -p {{instruction_q}}",
    )
    dto = await LLMProviderService().create(req, user=_user("org-a"))
    assert dto.cli_command_template == "claude -p {{instruction_q}}"


async def test_update_refills_a_cleared_cli_template():
    _seed(
        uid="mine-1", org_uid="org-a", kind="claude_subscription",
        cli_command_template="claude -p {{instruction_q}}",
    )
    dto = await LLMProviderService().update(
        "mine-1", UpdateLLMProviderRequest(cli_command_template=""), user=_user("org-a")
    )
    assert dto.cli_command_template == default_cli_template(LLMProviderKind.CLAUDE_SUBSCRIPTION)


async def test_create_with_bare_kind_fills_catalog_defaults():
    # The connect dialog sends as little as {kind}; the row must come out
    # labelled, addressed, and dispatchable.
    dto = await LLMProviderService().create(
        CreateLLMProviderRequest(kind=LLMProviderKind.LMSTUDIO), user=_user("org-a")
    )
    assert dto.label == "LM Studio"
    assert dto.base_url == "http://host.docker.internal:1234/v1"
    assert dto.model == KIND_CATALOG[LLMProviderKind.LMSTUDIO]["default_model"]

    api = await LLMProviderService().create(
        CreateLLMProviderRequest(kind=LLMProviderKind.OPENAI_API), user=_user("org-a")
    )
    assert api.api_key_env == "OPENAI_API_KEY"


async def test_create_explicit_values_beat_catalog_defaults():
    dto = await LLMProviderService().create(
        CreateLLMProviderRequest(
            kind=LLMProviderKind.OLLAMA, label="My box",
            base_url="http://box:1234/v1", model="m1",
        ),
        user=_user("org-a"),
    )
    assert (dto.label, dto.base_url, dto.model) == ("My box", "http://box:1234/v1", "m1")


def test_picker_features_exactly_the_user_facing_kinds():
    hidden = {k for k, m in KIND_CATALOG.items() if not m["featured"]}
    assert hidden == {LLMProviderKind.AIDER, LLMProviderKind.CUSTOM}
    order = [m["featured"] for m in KIND_CATALOG.values() if m["featured"]]
    assert sorted(order) == list(range(1, 9))  # unique, contiguous picker order


# ── Status probe (onboarding) ────────────────────────────────────────────────


async def test_status_reports_unconfigured_fresh_org():
    status = await LLMProviderService().status("org-a")
    assert status == {
        "configured": False, "provider_count": 0, "active_uid": "", "active_label": "",
    }


async def test_status_reports_the_active_provider():
    _seed(uid="mine-1", org_uid="org-a", label="Mine", active=True)
    _seed(uid="mine-2", org_uid="org-a")
    _seed(uid="theirs-1", org_uid="org-b", active=True)
    status = await LLMProviderService().status("org-a")
    assert status == {
        "configured": True, "provider_count": 2,
        "active_uid": "mine-1", "active_label": "Mine",
    }


# ── Pure chain unchanged ─────────────────────────────────────────────────────


def test_choose_provider_still_prefers_first_active():
    a = _Node(uid="a", active=True)
    b = _Node(uid="b", active=True)
    assert choose_provider([a, b]).uid == "a"
