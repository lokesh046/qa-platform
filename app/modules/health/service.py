"""Health logic: build the liveness and readiness reports.

Kept out of ``router.py`` so it can be tested without an HTTP client, and so
the decision about what readiness *means* lives in one readable place.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app import __version__
from app.core.config import Settings
from app.core.database import check_database_connection
from app.core.redis import check_redis_connection
from app.modules.health.schemas import (
    CheckStatus,
    DependencyCheck,
    HealthRead,
    ReadinessRead,
)

#: Probe name -> (callable, target description). The order fixes the order of
#: the ``checks`` array in the response, which keeps the output diffable.
_Probe = tuple[str, Callable[[], Awaitable[bool]], str]


def build_health(settings: Settings) -> HealthRead:
    """Describe this process without touching any dependency.

    Pure and synchronous. A liveness probe must answer even while Postgres is
    down, because the correct response to an unreachable database is to wait,
    not to restart the container.
    """
    return HealthRead(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
    )


async def _timed_probe(
    name: str,
    probe: Callable[[], Awaitable[bool]],
    target: str | None,
) -> DependencyCheck:
    """Run one probe, recording how long it took.

    The probe itself is expected to swallow its exception and return False,
    having logged the reason. The ``except`` here is a backstop: a probe that
    raises anyway must still produce a check result rather than taking down
    the whole endpoint.
    """
    started = time.perf_counter()
    try:
        ok = await probe()
    except Exception:  # a probe must never break the endpoint
        ok = False
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return DependencyCheck(
        name=name,
        status=CheckStatus.OK if ok else CheckStatus.FAILED,
        duration_ms=duration_ms,
        target=target,
    )


async def build_readiness(settings: Settings) -> ReadinessRead:
    """Probe every dependency and summarise the result.

    Probes run concurrently: readiness should cost about as long as the
    slowest dependency, not the sum of all of them.

    ``target`` is populated only outside staging and production. Locally it is
    the single most useful field on this endpoint, because it reports the
    database the process *actually reached* rather than the one the developer
    believes is configured. That distinction cost real time during Step 3,
    when a second project's Postgres was holding port 5432. In a deployment it
    is withheld: ``/ready`` is unauthenticated, and internal hostnames are
    reconnaissance. See ADR-007.
    """
    expose_target = not settings.is_deployment
    probes: list[_Probe] = [
        ("database", check_database_connection, settings.database_url_safe),
        ("redis", check_redis_connection, settings.redis_url_safe),
    ]

    checks = await asyncio.gather(
        *(
            _timed_probe(name, probe, target if expose_target else None)
            for name, probe, target in probes
        )
    )

    all_ok = all(check.status is CheckStatus.OK for check in checks)
    return ReadinessRead(status="ready" if all_ok else "not_ready", checks=list(checks))
