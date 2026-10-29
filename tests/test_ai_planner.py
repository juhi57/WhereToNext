"""The Claude integration, exercised against a stubbed SDK client.

No network, no API key, no spend. What is actually under test is the contract
around the call: that the request is shaped correctly, that every failure mode
becomes a typed domain error rather than a stack trace, and that the service
degrades to the template planner instead of returning a 500.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest

from app.config import Settings
from app.core.errors import AIUnavailableError, PlanGenerationError
from app.models.draft import ItineraryDraft
from app.models.enums import PlannerMode
from app.models.trip import TripRequest
from app.services.ai.planner import AIPlanner
from app.services.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from app.services.budget import BudgetEngine
from app.services.heuristic import HeuristicPlanner
from app.services.planner_service import PlannerService
from app.services.repository import Database, ItineraryRepository

# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read


class FakeMessage:
    """Mimics the SDK's ParsedMessage closely enough for the planner."""

    def __init__(
        self,
        parsed_output: ItineraryDraft | None,
        stop_reason: str = "end_turn",
        stop_details: object | None = None,
    ) -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = FakeUsage(1200, 4800, 900)


class FakeStopDetails:
    def __init__(self, category: str) -> None:
        self.type = "refusal"
        self.category = category


class FakeStream:
    def __init__(self, message: FakeMessage | None, error: Exception | None) -> None:
        self._message = message
        self._error = error

    async def __aenter__(self) -> FakeStream:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def get_final_message(self) -> FakeMessage:
        assert self._message is not None
        return self._message


class FakeMessages:
    def __init__(self, message: FakeMessage | None = None, error: Exception | None = None) -> None:
        self._message = message
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return FakeStream(self._message, self._error)


class FakeClient:
    def __init__(self, message: FakeMessage | None = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(message, error)


def http_response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com"))


def http_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def ai_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"anthropic_api_key": "sk-ant-test", "force_heuristic": False}
    )


@pytest.fixture
def draft(heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest) -> ItineraryDraft:
    """A valid draft, standing in for whatever the model would have returned."""
    return heuristic.draft(trip, engine.envelope(trip))


def planner_with(settings: Settings, client: FakeClient) -> AIPlanner:
    return AIPlanner(settings, client=client)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_sends_the_expected_request(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        client = FakeClient(FakeMessage(draft))
        await planner_with(ai_settings, client).draft(
            trip, engine.envelope(trip), engine.baseline(trip)
        )

        call = client.messages.calls[0]
        assert call["model"] == ai_settings.anthropic_model
        assert call["output_format"] is ItineraryDraft
        assert call["output_config"] == {"effort": ai_settings.effort}
        assert call["max_tokens"] == ai_settings.max_tokens
        assert call["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_system_prompt_is_marked_cacheable(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        """The system prompt is identical per request, so it must sit behind a
        cache breakpoint - that is most of the per-request cost."""
        client = FakeClient(FakeMessage(draft))
        await planner_with(ai_settings, client).draft(
            trip, engine.envelope(trip), engine.baseline(trip)
        )

        system = client.messages.calls[0]["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_usage_is_reported_back(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        result = await planner_with(ai_settings, FakeClient(FakeMessage(draft))).draft(
            trip, engine.envelope(trip), engine.baseline(trip)
        )
        assert result.input_tokens == 1200
        assert result.output_tokens == 4800
        assert result.cache_read_tokens == 900
        assert result.duration_ms >= 0

    def test_missing_key_fails_fast(self, settings: Settings) -> None:
        """Better a clear error at construction than a confusing one per request."""
        with pytest.raises(AIUnavailableError, match="No Anthropic API key"):
            AIPlanner(settings.model_copy(update={"anthropic_api_key": None}))


class TestFailureModes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (
                anthropic.AuthenticationError("bad key", response=http_response(401), body=None),
                "rejected",
            ),
            (
                anthropic.RateLimitError("slow down", response=http_response(429), body=None),
                "rate limited",
            ),
            (anthropic.APITimeoutError(request=http_request()), "too long"),
            (anthropic.APIConnectionError(request=http_request()), "Could not reach"),
            (
                anthropic.BadRequestError("bad", response=http_response(400), body=None),
                "rejected the planning request",
            ),
        ],
    )
    async def test_sdk_errors_become_domain_errors(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        error: Exception,
        expected: str,
    ) -> None:
        planner = planner_with(ai_settings, FakeClient(error=error))
        with pytest.raises(AIUnavailableError, match=expected):
            await planner.draft(trip, engine.envelope(trip), engine.baseline(trip))

    @pytest.mark.asyncio
    async def test_a_refusal_is_surfaced_with_its_category(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        message = FakeMessage(draft, stop_reason="refusal", stop_details=FakeStopDetails("cyber"))
        planner = planner_with(ai_settings, FakeClient(message))
        with pytest.raises(AIUnavailableError, match="declined") as exc:
            await planner.draft(trip, engine.envelope(trip), engine.baseline(trip))
        assert "cyber" in (exc.value.detail or "")

    @pytest.mark.asyncio
    async def test_a_truncated_response_is_rejected(
        self,
        ai_settings: Settings,
        engine: BudgetEngine,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        """Hitting max_tokens means the JSON is cut off and cannot be trusted."""
        planner = planner_with(
            ai_settings, FakeClient(FakeMessage(draft, stop_reason="max_tokens"))
        )
        with pytest.raises(AIUnavailableError, match="cut off"):
            await planner.draft(trip, engine.envelope(trip), engine.baseline(trip))

    @pytest.mark.asyncio
    async def test_an_unparseable_response_is_rejected(
        self, ai_settings: Settings, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        planner = planner_with(ai_settings, FakeClient(FakeMessage(None)))
        with pytest.raises(AIUnavailableError, match="did not match"):
            await planner.draft(trip, engine.envelope(trip), engine.baseline(trip))


class TestPrompt:
    def test_user_prompt_carries_the_brief_and_the_targets(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        prompt = build_user_prompt(trip, engine.envelope(trip), engine.baseline(trip))
        assert trip.origin in prompt
        assert trip.destination in prompt
        assert trip.start_date.isoformat() in prompt
        assert "vegetarian" in prompt
        assert "per night" in prompt
        assert "per person per day" in prompt

    def test_optional_details_appear_only_when_given(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        plain = build_user_prompt(trip, engine.envelope(trip), engine.baseline(trip))
        assert "Accessibility needs" not in plain

        with_needs = trip.model_copy(update={"accessibility_needs": "step-free access"})
        detailed = build_user_prompt(
            with_needs, engine.envelope(with_needs), engine.baseline(with_needs)
        )
        assert "step-free access" in detailed

    def test_an_infeasible_budget_is_called_out(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        broke = trip.model_copy(update={"budget_total": 900})
        prompt = build_user_prompt(broke, engine.envelope(broke), engine.baseline(broke))
        assert "does not realistically cover" in prompt

    def test_system_prompt_forbids_arithmetic(self) -> None:
        """The whole cost design rests on the model not computing totals."""
        assert "Never compute a total" in SYSTEM_PROMPT

    def test_system_prompt_is_stable(self) -> None:
        """It must contain nothing per-request, or prompt caching never hits."""
        for volatile in ("{", "}", "Mumbai", "2026"):
            assert volatile not in SYSTEM_PROMPT


class TestServiceFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_templates_when_the_model_fails(
        self, ai_settings: Settings, trip: TripRequest
    ) -> None:
        database = Database(ai_settings.database_url)
        await database.create_schema()
        try:
            planner = AIPlanner(
                ai_settings,
                client=FakeClient(error=anthropic.APIConnectionError(request=http_request())),  # type: ignore[arg-type]
            )
            service = PlannerService(ai_settings, ItineraryRepository(database), ai_planner=planner)

            itinerary = (await service.create_plan(trip)).itinerary
            assert itinerary.meta.mode is PlannerMode.HEURISTIC
            assert len(itinerary.days) == trip.duration_days
            assert any("unavailable" in note for note in itinerary.meta.adjustments)
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_fallback_can_be_disabled(self, ai_settings: Settings, trip: TripRequest) -> None:
        strict = ai_settings.model_copy(update={"fallback_to_heuristic": False})
        database = Database(strict.database_url)
        await database.create_schema()
        try:
            planner = AIPlanner(
                strict,
                client=FakeClient(error=anthropic.APIConnectionError(request=http_request())),  # type: ignore[arg-type]
            )
            service = PlannerService(strict, ItineraryRepository(database), ai_planner=planner)
            with pytest.raises(PlanGenerationError):
                await service.create_plan(trip)
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_a_successful_call_is_reconciled_like_any_other_draft(
        self,
        ai_settings: Settings,
        trip: TripRequest,
        draft: ItineraryDraft,
    ) -> None:
        """AI output goes through the same cost engine as template output."""
        database = Database(ai_settings.database_url)
        await database.create_schema()
        try:
            poisoned = draft.model_copy(deep=True)
            poisoned.days[0].stay.estimated_cost_per_night_total = 9_000_000

            planner = AIPlanner(ai_settings, client=FakeClient(FakeMessage(poisoned)))  # type: ignore[arg-type]
            service = PlannerService(ai_settings, ItineraryRepository(database), ai_planner=planner)

            itinerary = (await service.create_plan(trip)).itinerary
            assert itinerary.meta.mode is PlannerMode.AI
            assert itinerary.meta.input_tokens == 1200
            assert itinerary.meta.cache_hit is True
            assert any("implausibly high" in note for note in itinerary.meta.adjustments)
            assert itinerary.budget.total < 1_000_000
        finally:
            await database.dispose()

    def test_service_runs_without_a_key(self, settings: Settings) -> None:
        database = Database(settings.database_url)
        service = PlannerService(settings, ItineraryRepository(database))
        assert service.ai_enabled is False
