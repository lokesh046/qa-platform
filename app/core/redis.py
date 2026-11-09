"""Redis client lifecycle.

Shaped deliberately like :mod:`app.core.database`: one lazily-created,
process-wide client, a readiness probe, and an explicit close on shutdown.
Phase 1 only reads Redis to prove it is reachable. Later phases use it for the
job queue and for caching, and they should not have to invent a client.

Why lazy: importing this module must not open a socket. Tests import the
application without a Redis running, and a module-level client would make
every such import fail.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import Settings, get_settings

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """Return the process-wide Redis client, building it on first call.

    ``decode_responses=True`` so callers get ``str`` rather than ``bytes``;
    everything this project stores in Redis is text.

    The two timeouts matter more than they look. Without
    ``socket_connect_timeout`` a readiness probe aimed at a black-holed address
    hangs until the kernel gives up, which can exceed the probe's own deadline
    and make a reachable service look dead.
    """
    settings: Settings = get_settings()
    timeout = settings.redis_timeout_seconds
    _logger.info("creating redis client", extra={"redis_url": settings.redis_url_safe})
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
    )


async def check_redis_connection() -> bool:
    """Return True if Redis answers a PING.

    Used by ``/ready``. Returns a bool rather than raising so the endpoint can
    report every dependency's state in one response instead of failing on the
    first one. The reason is logged, not returned: a connection error can
    include the host and, for a URL with credentials, the username.
    """
    try:
        await get_redis().ping()
    except Exception:
        _logger.exception("redis readiness check failed")
        return False
    return True


async def close_redis() -> None:
    """Close the pooled connections. Called on application shutdown.

    Skipped when no client was ever built, so shutdown does not create one just
    to close it.
    """
    if get_redis.cache_info().currsize == 0:
        return
    await get_redis().aclose()
    _logger.info("redis client closed")


def reset_redis_cache() -> None:
    """Drop the cached client, for tests that point at another Redis.

    Call ``close_redis()`` first if a client may already be open.
    """
    get_redis.cache_clear()
