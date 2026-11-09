"""HTTP endpoints for liveness and readiness.

Thin by design: parse nothing, decide nothing, delegate to ``service.py`` and
choose a status code.

Both endpoints are unauthenticated. A Kubernetes probe and a Compose
healthcheck cannot carry a bearer token, and neither endpoint reveals anything
secret. See ADR-007.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.core.config import Settings, get_settings
from app.core.errors import ResponseEnvelope, ServiceUnavailableError, success_payload
from app.modules.health.schemas import HealthRead, ReadinessRead
from app.modules.health.service import build_health, build_readiness

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=ResponseEnvelope[HealthRead],
    summary="Liveness probe",
    status_code=status.HTTP_200_OK,
)
async def get_health(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Report that the process is alive.

    Never fails while the process can answer, and never touches a dependency.
    """
    return success_payload(build_health(settings))


@router.get(
    "/ready",
    response_model=ResponseEnvelope[ReadinessRead],
    summary="Readiness probe",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "At least one dependency is unreachable. The per-dependency results are "
                "in `error.details`."
            ),
            "model": ResponseEnvelope[ReadinessRead],
        }
    },
)
async def get_ready(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Report whether every dependency this service needs is reachable.

    Returns 200 when all probes pass. When any probe fails it raises, which
    the handler renders as 503 with code ``service_unavailable`` and the full
    check list under ``error.details``.

    Raising rather than returning a 503 body directly keeps one rule true
    across the whole API: a non-2xx response always carries ``error`` and a
    null ``data``. The cost is that the check list moves from ``data`` to
    ``error.details`` on failure, which is documented on the endpoint.
    """
    report = await build_readiness(settings)
    if report.status == "not_ready":
        failed = [check.name for check in report.checks if check.status != "ok"]
        raise ServiceUnavailableError(
            f"Service is not ready: {', '.join(failed)} unreachable.",
            details=report.model_dump(mode="json"),
        )
    return success_payload(report)
