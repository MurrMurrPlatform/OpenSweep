"""Redis URL builder — reads from pydantic Settings (config.settings).

Settings loads `.env` itself, so REDIS_* values set there are honored even
though pydantic-settings never exports them into os.environ.

TLS: a non-empty REDIS_ACCESS_KEY means the broker is reached over a network
the application does not own, so `rediss://` is built with the server
certificate REQUIRED. Verification is never disabled — the AUTH password
travels in-band on this connection and Celery task payloads (run dispatch
instructions) travel through it, so an unverified handshake would let an
on-path attacker read the credential and rewrite the work queue. A private CA
is configured with REDIS_CA_CERTS rather than by skipping the check.
"""

import ssl
from urllib.parse import quote

from config import settings

# redis-py parses `ssl_cert_reqs` off the URL query string; "required" maps to
# ssl.CERT_REQUIRED. Celery takes its SSL options out-of-band instead (a dict
# of ssl.* constants), so both spellings of the same policy live here.
_SSL_CERT_REQS = "required"


def _tls_query() -> str:
    params = [f"ssl_cert_reqs={_SSL_CERT_REQS}"]
    if settings.REDIS_CA_CERTS:
        params.append(f"ssl_ca_certs={quote(settings.REDIS_CA_CERTS, safe='')}")
    return "&".join(params)


def get_redis_url(db: int = 0) -> str:
    host = settings.REDIS_HOST
    port = settings.REDIS_PORT
    access_key = settings.REDIS_ACCESS_KEY
    if access_key:
        return f"rediss://:{access_key}@{host}:{port}/{db}?{_tls_query()}"
    return f"redis://{host}:{port}/{db}"


def get_masked_redis_url(db: int = 0) -> str:
    host = settings.REDIS_HOST
    port = settings.REDIS_PORT
    access_key = settings.REDIS_ACCESS_KEY
    if access_key:
        return f"rediss://:<MASKED>@{host}:{port}/{db}?{_tls_query()}"
    return f"redis://{host}:{port}/{db}"


def get_redis_ssl_options() -> dict | None:
    """SSL kwargs for clients configured out-of-band (Celery's
    `broker_use_ssl` / `redis_backend_use_ssl`), mirroring the URL query
    string built above. None when TLS is not in play."""
    if not settings.REDIS_ACCESS_KEY:
        return None
    options: dict = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    if settings.REDIS_CA_CERTS:
        options["ssl_ca_certs"] = settings.REDIS_CA_CERTS
    return options
