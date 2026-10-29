"""HTTP API.

Everything lives under ``/api``. The routes are thin: validate, delegate to a
service, serialise. No business logic here - if a route grows a calculation,
that calculation belongs in :mod:`app.services`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import PlannerDep, RateLimited, RepositoryDep, SettingsDep
from app.core.logging import get_logger
from app.models.enums import (
    Currency,
    FoodPreference,
    Interest,
    Pace,
    StayType,
    TransportMode,
)
from app.models.itinerary import Itinerary
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.pdf import render_itinerary_pdf

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["itineraries"])


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    """Liveness plus the facts worth knowing when something looks wrong."""

    status: Literal["ok"] = "ok"
    app: str
    environment: str
    ai_enabled: bool = Field(description="Whether a live model call is possible.")
    planner_model: str
    stored_itineraries: int
    cache: dict[str, int | float]


class PlanResponse(BaseModel):
    """A generated plan plus whether it came from the cache."""

    itinerary: Itinerary
    from_cache: bool


class EstimateResponse(BaseModel):
    """A fast, free cost estimate made without calling the model."""

    currency: Currency
    distance_km: float
    distance_estimated: bool
    price_tier: str
    international: bool
    lines: dict[str, float]
    total: float
    per_person: float
    budget_total: float
    utilisation_pct: float
    verdict: str
    advice: list[str]


class OptionsResponse(BaseModel):
    """Every controlled vocabulary the UI needs to build its form."""

    transport_modes: list[dict[str, str]]
    stay_types: list[dict[str, str]]
    food_preferences: list[dict[str, str]]
    paces: list[dict[str, str]]
    interests: list[dict[str, str]]
    currencies: list[dict[str, str]]
    destinations: list[str]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(settings: SettingsDep, planner: PlannerDep, repo: RepositoryDep) -> HealthResponse:
    """Service health and configuration summary."""
    return HealthResponse(
        app=settings.app_name,
        environment=settings.environment,
        ai_enabled=planner.ai_enabled,
        planner_model=settings.anthropic_model if planner.ai_enabled else "heuristic-v1",
        stored_itineraries=await repo.count(),
        cache=planner.cache_stats(),
    )


@router.get("/options", response_model=OptionsResponse, tags=["system"])
async def options() -> OptionsResponse:
    """Enumerations and destination suggestions for the booking form."""
    from app.services.cost_data import place_book

    def labelled(enum_cls: type[StrEnum]) -> list[dict[str, str]]:
        return [
            {"value": member.value, "label": member.value.replace("_", " ").title()}
            for member in enum_cls
        ]

    return OptionsResponse(
        transport_modes=labelled(TransportMode),
        stay_types=labelled(StayType),
        food_preferences=labelled(FoodPreference),
        paces=labelled(Pace),
        interests=labelled(Interest),
        currencies=[{"value": c.value, "label": c.value} for c in Currency],
        destinations=sorted(p.name for p in place_book().places),
    )


@router.post(
    "/estimate",
    response_model=EstimateResponse,
    tags=["itineraries"],
    summary="Cost a trip without generating an itinerary",
)
async def estimate(req: TripRequest) -> EstimateResponse:
    """Price a trip from reference data alone.

    Instant and free - no model call. Useful for showing the user whether their
    budget is realistic before they commit to generating a full plan.
    """
    engine = BudgetEngine()
    baseline = engine.baseline(req)
    summary = engine.summarise(req, baseline.as_lines(), baseline)
    return EstimateResponse(
        currency=req.currency,
        distance_km=baseline.distance_km,
        distance_estimated=baseline.distance_estimated,
        price_tier=baseline.tier,
        international=baseline.international,
        lines={k.value: v for k, v in baseline.as_lines().items()},
        total=summary.total,
        per_person=summary.per_person,
        budget_total=summary.budget_total,
        utilisation_pct=summary.utilisation_pct,
        verdict=summary.verdict.value,
        advice=summary.advice,
    )


@router.post(
    "/itineraries",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RateLimited],
    summary="Generate and store a full itinerary",
)
async def create_itinerary(
    req: TripRequest,
    planner: PlannerDep,
    refresh: Annotated[bool, Query(description="Bypass the cache and plan from scratch.")] = False,
) -> PlanResponse:
    """Plan the trip, reconcile its costs and save it."""
    outcome = await planner.create_plan(req, use_cache=not refresh)
    return PlanResponse(itinerary=outcome.itinerary, from_cache=outcome.from_cache)


@router.get("/itineraries", summary="List recently generated itineraries")
async def list_itineraries(
    repo: RepositoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """Summaries only - fetch an id for the full plan."""
    return await repo.list_recent(limit=limit, offset=offset)


@router.get(
    "/itineraries/{itinerary_id}",
    response_model=Itinerary,
    summary="Fetch one stored itinerary",
)
async def get_itinerary(itinerary_id: str, planner: PlannerDep) -> Itinerary:
    return await planner.get_plan(itinerary_id)


@router.get(
    "/itineraries/{itinerary_id}/pdf",
    response_class=Response,
    summary="Download the itinerary as a PDF",
    responses={200: {"content": {"application/pdf": {}}, "description": "The itinerary"}},
)
async def download_pdf(
    itinerary_id: str,
    planner: PlannerDep,
    inline: Annotated[
        bool, Query(description="Render in the browser instead of downloading.")
    ] = False,
) -> Response:
    """Render the stored itinerary to PDF.

    ReportLab is synchronous and CPU-bound. Rendering takes tens of
    milliseconds for a typical trip, which is short enough to do inline; if trip
    length ever makes that untrue, move this to a thread with
    ``anyio.to_thread.run_sync``.
    """
    itinerary = await planner.get_plan(itinerary_id)
    pdf = render_itinerary_pdf(itinerary)
    disposition = "inline" if inline else "attachment"
    logger.info("pdf_rendered", extra={"itinerary_id": itinerary_id, "bytes": len(pdf)})
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{itinerary.slug()}.pdf"',
            "Content-Length": str(len(pdf)),
            "Cache-Control": "private, max-age=300",
        },
    )


@router.delete(
    "/itineraries/{itinerary_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a stored itinerary",
)
async def delete_itinerary(itinerary_id: str, repo: RepositoryDep) -> Response:
    await repo.delete(itinerary_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
