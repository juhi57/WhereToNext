"""Shared fixtures.

Every test runs against an in-memory database and the template planner, so the
suite is fast, hermetic and never makes a network call or spends money. The AI
path is covered in :mod:`tests.test_ai_planner` with a stubbed SDK client.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.enums import FoodPreference, Interest, Pace, StayType
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.heuristic import HeuristicPlanner


@pytest.fixture
def settings() -> Settings:
    """Hermetic settings: in-memory DB, no AI, quiet logs."""
    return Settings(
        environment="test",
        log_level="ERROR",
        database_url="sqlite+aiosqlite:///:memory:",
        force_heuristic=True,
        anthropic_api_key=None,
        cache_ttl_seconds=0,  # disabled, so each test plans from scratch
        rate_limit_per_minute=100,
    )


@pytest.fixture
def start_date() -> date:
    """A departure comfortably in the future, so validation never trips on it."""
    return date.today() + timedelta(days=45)


@pytest.fixture
def trip(start_date: date) -> TripRequest:
    """A representative six-day international trip."""
    return TripRequest(
        origin="Mumbai",
        destination="Bali, Indonesia",
        start_date=start_date,
        end_date=start_date + timedelta(days=5),
        adults=2,
        children=0,
        budget_total=180_000,
        stay_type=StayType.MID_HOTEL,
        food_preference=FoodPreference.VEGETARIAN,
        pace=Pace.BALANCED,
        interests=[Interest.BEACHES, Interest.FOOD, Interest.CULTURE],
    )


@pytest.fixture
def engine() -> BudgetEngine:
    return BudgetEngine()


@pytest.fixture
def heuristic(engine: BudgetEngine) -> HeuristicPlanner:
    return HeuristicPlanner(engine)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient with the app's lifespan run, so state is wired up."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def trip_payload(trip: TripRequest) -> dict[str, Any]:
    """The JSON body an API caller would send for ``trip``."""
    body = trip.model_dump(mode="json")
    for computed in TripRequest.model_computed_fields:
        body.pop(computed, None)
    return body
