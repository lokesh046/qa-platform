"""Response models for the health endpoints.

Output models, not input, but they still set ``extra="forbid"``. Nothing in
this project should be able to add a field to a response by accident.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import AppEnv


class CheckStatus(StrEnum):
    """Outcome of a single dependency probe."""

    OK = "ok"
    FAILED = "failed"


class HealthRead(BaseModel):
    """Answer from ``/health``: this process is alive.

    Contains no dependency state on purpose. See ADR-007.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = Field(description="Always 'ok'. A dead process cannot answer at all.")
    service: str = Field(description="Configured application name.")
    version: str = Field(description="Application version.")
    environment: AppEnv = Field(description="Which environment this process believes it is in.")


class DependencyCheck(BaseModel):
    """Result of probing one dependency."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Dependency name, for example 'database' or 'redis'.")
    status: CheckStatus = Field(description="Whether the probe succeeded.")
    duration_ms: float = Field(description="How long the probe took, in milliseconds.")
    target: str | None = Field(
        default=None,
        description=(
            "Which host the probe actually reached, with any password masked. "
            "Null in staging and production, where internal topology is not published."
        ),
    )


class ReadinessRead(BaseModel):
    """Answer from ``/ready``: whether this process can serve traffic."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"] = Field(description="Aggregate readiness.")
    checks: list[DependencyCheck] = Field(description="One entry per dependency, in a fixed order.")
