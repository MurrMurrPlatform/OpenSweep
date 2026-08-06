"""`policy_resolver._check_routing` is the run-start gate for locality.

Regression for the "local_only lets hosted opencode through" gap: locality
must be a property of the resolved provider's `base_url`, NOT the executor
kind. An opencode row pointed at a hosted OpenAI-compatible endpoint (Azure
Foundry, OpenRouter, …) is metered even though the kind matches a local
CLI, so `local_only=True` and `cloud_allowed=False` must refuse it.
"""

from types import SimpleNamespace

import pytest

from domains.llm_providers.services.llm_executor import (
    is_local_base_url,
    is_local_provider,
)
from domains.run_policies.services.policy_resolver import (
    PolicyViolation,
    _check_routing,
)
from domains.runs.schemas import Executor


def _policy(**overrides):
    base = dict(
        local_only=False,
        cloud_allowed=True,
        allowed_executors=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider(kind: str, base_url: str = ""):
    return SimpleNamespace(kind=kind, base_url=base_url)


# ── is_local_base_url ─────────────────────────────────────────────────────


class TestIsLocalBaseUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:2345/v1",
            "http://127.0.0.1:1234/v1",
            "http://host.docker.internal:11434/v1",  # the seed default
            "http://[::1]:8080",
            "http://10.0.0.5:8080",  # RFC1918 LAN
            "http://192.168.1.7:8080",
            "http://172.16.5.9:8080",
            "http://box.local:8080",
            "http://box.internal:8080",
        ],
    )
    def test_local_hosts(self, url):
        assert is_local_base_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "",  # empty → not local; ambient container config could point anywhere
            "https://api.openai.com/v1",
            "https://openrouter.ai/api/v1",
            "https://foo.cognitiveservices.azure.com/openai/v1",
            "http://8.8.8.8/v1",  # public IP
            "https://custom-proxy.example.com/v1",
        ],
    )
    def test_hosted_hosts(self, url):
        assert is_local_base_url(url) is False


# ── is_local_provider ─────────────────────────────────────────────────────


class TestIsLocalProvider:
    def test_none_provider_is_not_local(self):
        assert is_local_provider(None) is False

    def test_claude_subscription_is_never_local(self):
        """claude_subscription always hits Anthropic's hosted API; base_url is
        ignored because the CLI does not use it for that kind."""
        assert is_local_provider(_provider("claude_subscription", "http://localhost:1234")) is False

    def test_opencode_local_endpoint_is_local(self):
        assert is_local_provider(_provider("opencode", "http://localhost:2345/v1")) is True
        assert (
            is_local_provider(_provider("opencode", "http://host.docker.internal:11434/v1"))
            is True
        )

    def test_opencode_hosted_endpoint_is_not_local(self):
        """The exact case the gap covered: an opencode row pointed at Azure
        Foundry / OpenRouter must NOT be classified as local."""
        assert (
            is_local_provider(
                _provider("opencode", "https://foo.cognitiveservices.azure.com/openai/v1")
            )
            is False
        )
        assert (
            is_local_provider(_provider("opencode", "https://openrouter.ai/api/v1")) is False
        )

    def test_opencode_with_empty_base_url_is_not_local(self):
        """Ambient container config could point anywhere — do not silently
        grant the local privileges to an unconfigured row."""
        assert is_local_provider(_provider("opencode", "")) is False


# ── _check_routing ────────────────────────────────────────────────────────


class TestCheckRoutingLocalOnly:
    def test_local_only_blocks_claude_subscription(self):
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(local_only=True, cloud_allowed=False),
                Executor.CLAUDE_CODE,
                _provider("claude_subscription"),
            )
        assert exc.value.code == "routing_local_only"

    def test_local_only_blocks_hosted_opencode(self):
        """The core regression: an opencode provider whose base_url is a
        hosted OpenAI-compatible endpoint (Azure Foundry, OpenRouter, …)
        must be refused under local_only=True."""
        for base_url in (
            "https://foo.cognitiveservices.azure.com/openai/v1",
            "https://openrouter.ai/api/v1",
            "https://api.together.xyz/v1",
        ):
            with pytest.raises(PolicyViolation) as exc:
                _check_routing(
                    _policy(local_only=True, cloud_allowed=False),
                    Executor.OPENCODE,
                    _provider("opencode", base_url),
                )
            assert exc.value.code == "routing_local_only", base_url

    def test_local_only_blocks_opencode_without_base_url(self):
        """Ambient config could point anywhere — refuse rather than silently
        permit."""
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(local_only=True, cloud_allowed=False),
                Executor.OPENCODE,
                _provider("opencode", ""),
            )
        assert exc.value.code == "routing_local_only"

    def test_local_only_blocks_when_provider_unknown(self):
        """Missing provider → cloud (safe default); local_only refuses."""
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(local_only=True, cloud_allowed=False),
                Executor.OPENCODE,
                None,
            )
        assert exc.value.code == "routing_local_only"

    def test_local_only_permits_local_opencode(self):
        """opencode against an on-machine server is the exact case
        local_only is designed to permit."""
        # No exception raised.
        _check_routing(
            _policy(local_only=True, cloud_allowed=False),
            Executor.OPENCODE,
            _provider("opencode", "http://localhost:2345/v1"),
        )
        _check_routing(
            _policy(local_only=True, cloud_allowed=False),
            Executor.OPENCODE,
            _provider("opencode", "http://host.docker.internal:11434/v1"),
        )


class TestCheckRoutingCloudAllowed:
    def test_cloud_disallowed_blocks_hosted_opencode(self):
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(cloud_allowed=False),
                Executor.OPENCODE,
                _provider("opencode", "https://openrouter.ai/api/v1"),
            )
        assert exc.value.code == "routing_cloud_blocked"

    def test_cloud_disallowed_blocks_claude_subscription(self):
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(cloud_allowed=False),
                Executor.CLAUDE_CODE,
                _provider("claude_subscription"),
            )
        assert exc.value.code == "routing_cloud_blocked"

    def test_cloud_disallowed_permits_local_opencode(self):
        _check_routing(
            _policy(cloud_allowed=False),
            Executor.OPENCODE,
            _provider("opencode", "http://localhost:2345/v1"),
        )

    def test_cloud_allowed_permits_hosted_opencode(self):
        _check_routing(
            _policy(cloud_allowed=True),
            Executor.OPENCODE,
            _provider("opencode", "https://openrouter.ai/api/v1"),
        )


class TestAllowedExecutors:
    def test_allowlist_refuses_out_of_list(self):
        with pytest.raises(PolicyViolation) as exc:
            _check_routing(
                _policy(allowed_executors=["claude_code"]),
                Executor.OPENCODE,
                _provider("opencode", "http://localhost:2345/v1"),
            )
        assert exc.value.code == "routing_not_in_allowlist"

    def test_allowlist_permits_listed(self):
        _check_routing(
            _policy(allowed_executors=["claude_code", "opencode"]),
            Executor.OPENCODE,
            _provider("opencode", "http://localhost:2345/v1"),
        )

    def test_empty_allowlist_allows_all(self):
        _check_routing(
            _policy(allowed_executors=[]),
            Executor.OPENCODE,
            _provider("opencode", "http://localhost:2345/v1"),
        )

    def test_whitespace_in_allowlist_is_ignored(self):
        """`allowed_executors` is JSON free-text on the DTO — stray whitespace
        or blank entries must not silently narrow the allowlist to nothing."""
        _check_routing(
            _policy(allowed_executors=["", "  ", "opencode"]),
            Executor.OPENCODE,
            _provider("opencode", "http://localhost:2345/v1"),
        )
