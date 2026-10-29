"""FastAPI dependencies.

Shared objects (settings, database, planner) are built once in the lifespan
handler and hung off ``app.state``; these functions hand them to route
signatures. Doing it this way rather than with module-level globals is what lets
the tests build an app with an in-memory database and a stubbed planner.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.core.errors import RateLimitedError
from app.core.rate_limit import SlidingWindowLimiter
from app.services.planner_service import PlannerService
from app.services.repository import ItineraryRepository


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_planner(request: Request) -> PlannerService:
    return request.app.state.planner  # type: ignore[no-any-return]


def get_repository(request: Request) -> ItineraryRepository:
    return request.app.state.repository  # type: ignore[no-any-return]


def client_fingerprint(request: Request) -> str:
    """Best-effort client identity for rate limiting.

    Honours ``X-Forwarded-For`` because the app is expected to sit behind a
    reverse proxy. That header is client-controlled and trivially spoofed, so it
    is used only to spread load fairly, never for authorisation.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    """Reject callers who exceed the planning quota.

    Applied only to plan creation - the one route that costs a model call.
    """
    limiter: SlidingWindowLimiter = request.app.state.limiter
    allowed, retry_after = limiter.check(client_fingerprint(request))
    if not allowed:
        raise RateLimitedError(
            "Too many planning requests. Please wait a moment and try again.",
            retry_after=retry_after,
        )


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
PlannerDep = Annotated[PlannerService, Depends(get_planner)]
RepositoryDep = Annotated[ItineraryRepository, Depends(get_repository)]
RateLimited = Depends(enforce_rate_limit)
