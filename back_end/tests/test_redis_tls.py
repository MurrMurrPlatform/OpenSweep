"""Redis TLS must VERIFY the server certificate.

`REDIS_ACCESS_KEY` being set is exactly the deployment shape where this
matters — a managed Redis reached over a network the app does not own. The
AUTH password travels in-band on that connection and Celery task payloads
(run-dispatch instructions) travel through the broker, so an unverified
handshake hands both to an on-path attacker. These tests pin that no
verification-disabling value can come back.
"""

import ssl
from pathlib import Path

import pytest

import celery_app as celery_module
import redis_config
from config import settings
from redis_config import get_masked_redis_url, get_redis_ssl_options, get_redis_url


@pytest.fixture
def tls_redis(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_HOST", "redis.example.com")
    monkeypatch.setattr(settings, "REDIS_PORT", 6380)
    monkeypatch.setattr(settings, "REDIS_ACCESS_KEY", "s3cret")
    monkeypatch.setattr(settings, "REDIS_CA_CERTS", "")


def test_tls_url_requires_cert_verification(tls_redis):
    url = get_redis_url(0)
    assert url.startswith("rediss://")
    assert "ssl_cert_reqs=required" in url
    assert "ssl_cert_reqs=none" not in url
    assert "ssl_cert_reqs=optional" not in url


def test_masked_url_mirrors_the_real_url(tls_redis):
    real = get_redis_url(2)
    masked = get_masked_redis_url(2)
    assert masked == real.replace("s3cret", "<MASKED>")
    assert "s3cret" not in masked
    assert "ssl_cert_reqs=required" in masked


def test_private_ca_is_configured_not_skipped(tls_redis, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_CA_CERTS", "/etc/ssl/certs/redis ca.pem")
    url = get_redis_url(0)
    assert "ssl_cert_reqs=required" in url
    # Path is percent-encoded so a space (or any reserved char) cannot break
    # the query string apart.
    assert "ssl_ca_certs=%2Fetc%2Fssl%2Fcerts%2Fredis%20ca.pem" in url


def test_plain_redis_when_no_access_key(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ACCESS_KEY", "")
    assert get_redis_url(0).startswith("redis://")
    assert get_redis_ssl_options() is None


def test_celery_ssl_options_require_certs(tls_redis):
    options = get_redis_ssl_options()
    assert options is not None
    assert options["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert options["ssl_cert_reqs"] != ssl.CERT_NONE
    assert "ssl_ca_certs" not in options


def test_celery_ssl_options_carry_the_ca_bundle(tls_redis, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_CA_CERTS", "/etc/ssl/certs/redis-ca.pem")
    options = get_redis_ssl_options()
    assert options["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert options["ssl_ca_certs"] == "/etc/ssl/certs/redis-ca.pem"


def test_celery_app_feeds_the_same_options_to_broker_and_backend():
    # Both are built from get_redis_ssl_options() at import time, and the two
    # dicts must not be the same object — Celery mutates each independently.
    assert celery_module.app.conf.broker_use_ssl == celery_module.broker_use_ssl
    assert celery_module.app.conf.redis_backend_use_ssl == celery_module.redis_backend_use_ssl
    assert celery_module.broker_use_ssl == celery_module.redis_backend_use_ssl
    if celery_module.broker_use_ssl is not None:
        assert celery_module.broker_use_ssl is not celery_module.redis_backend_use_ssl


@pytest.mark.parametrize("module", [redis_config, celery_module])
def test_no_verification_disabling_value_in_source(module):
    """The dicts above are built at import time, so a regression that
    reintroduces CERT_NONE would not be visible to a runtime assertion in a
    test process where REDIS_ACCESS_KEY is unset. Guard the source instead."""
    source = Path(module.__file__).read_text()
    assert "CERT_NONE" not in source
    assert "ssl_cert_reqs=none" not in source
