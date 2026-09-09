from __future__ import annotations

from celery import Celery

from app.config import settings


def _redis_url_with_ssl(url: str) -> str:
    """Append ssl_cert_reqs=CERT_NONE to rediss:// URLs that lack it.

    Celery requires this parameter to be explicit when the scheme is rediss://.
    Azure Cache for Redis uses managed TLS certificates, so CERT_NONE is the
    standard setting used in PagoPA environments.
    """
    if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}ssl_cert_reqs=CERT_NONE"
    return url


_broker_url = _redis_url_with_ssl(settings.redis_url)

celery_app = Celery(
    "qachub",
    broker=_broker_url,
    backend=_broker_url,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
