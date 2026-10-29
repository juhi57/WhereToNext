"""Cache, rate limiter and repository."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.config import Settings, get_settings
from app.core.cache import TTLCache
from app.core.errors import ItineraryNotFoundError
from app.core.rate_limit import SlidingWindowLimiter
from app.models.enums import Currency
from app.models.itinerary import Itinerary
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.heuristic import HeuristicPlanner
from app.services.planner_service import PlannerService
from app.services.repository import Database, ItineraryRepository


class TestTTLCache:
    def test_stores_and_returns_values(self) -> None:
        cache: TTLCache[str] = TTLCache(ttl_seconds=60)
        cache.set("k", "v")
        assert cache.get("k") == "v"

    def test_missing_keys_return_none(self) -> None:
        assert TTLCache[str](ttl_seconds=60).get("absent") is None

    def test_entries_expire(self) -> None:
        cache: TTLCache[str] = TTLCache(ttl_seconds=0.01)
        cache.set("k", "v")
        asyncio.run(asyncio.sleep(0.05))
        assert cache.get("k") is None

    def test_zero_ttl_disables_the_cache(self) -> None:
        cache: TTLCache[str] = TTLCache(ttl_seconds=0)
        cache.set("k", "v")
        assert cache.enabled is False
        assert cache.get("k") is None

    def test_least_recently_used_entries_are_evicted(self) -> None:
        cache: TTLCache[int] = TTLCache(ttl_seconds=60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # 'a' becomes the most recently used
        cache.set("c", 3)  # evicts 'b'
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3

    def test_stats_track_hits_and_misses(self) -> None:
        cache: TTLCache[int] = TTLCache(ttl_seconds=60)
        cache.set("a", 1)
        cache.get("a")
        cache.get("b")
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_clear_resets_everything(self) -> None:
        cache: TTLCache[int] = TTLCache(ttl_seconds=60)
        cache.set("a", 1)
        cache.get("a")
        cache.clear()
        assert cache.stats()["entries"] == 0
        assert cache.stats()["hits"] == 0


class TestRateLimiter:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = SlidingWindowLimiter(limit=3)
        assert [limiter.check("ip")[0] for _ in range(3)] == [True, True, True]

    def test_blocks_beyond_the_limit(self) -> None:
        limiter = SlidingWindowLimiter(limit=2)
        limiter.check("ip")
        limiter.check("ip")
        allowed, retry_after = limiter.check("ip")
        assert allowed is False
        assert retry_after > 0

    def test_keys_are_independent(self) -> None:
        limiter = SlidingWindowLimiter(limit=1)
        assert limiter.check("a")[0] is True
        assert limiter.check("b")[0] is True
        assert limiter.check("a")[0] is False

    def test_the_window_slides(self) -> None:
        limiter = SlidingWindowLimiter(limit=1, window_seconds=0.05)
        assert limiter.check("ip")[0] is True
        assert limiter.check("ip")[0] is False
        asyncio.run(asyncio.sleep(0.08))
        assert limiter.check("ip")[0] is True

    def test_reset_clears_a_key(self) -> None:
        limiter = SlidingWindowLimiter(limit=1)
        limiter.check("ip")
        limiter.reset("ip")
        assert limiter.check("ip")[0] is True

    def test_key_table_stays_bounded(self) -> None:
        """A long-lived process must not grow one deque per IP ever seen."""
        limiter = SlidingWindowLimiter(limit=5, max_keys=16)
        for n in range(200):
            limiter.check(f"ip-{n}")
        assert len(limiter._hits) <= 16

    def test_a_zero_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            SlidingWindowLimiter(limit=0)


@pytest_asyncio.fixture
async def repo(settings: Settings) -> AsyncIterator[ItineraryRepository]:
    database = Database(settings.database_url)
    await database.create_schema()
    try:
        yield ItineraryRepository(database)
    finally:
        await database.dispose()


@pytest.fixture
def itinerary(
    settings: Settings, engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest
) -> Itinerary:
    from app.models.enums import PlannerMode
    from app.models.itinerary import PlannerMeta

    draft = heuristic.draft(trip, engine.envelope(trip))
    plan = engine.reconcile(trip, draft)
    return Itinerary(
        request=trip,
        title=draft.title,
        outbound=plan.outbound,
        inbound=plan.inbound,
        days=plan.days,
        budget=plan.budget,
        meta=PlannerMeta(mode=PlannerMode.HEURISTIC),
    )


class TestRepository:
    @pytest.mark.asyncio
    async def test_save_and_fetch_round_trips(
        self, repo: ItineraryRepository, itinerary: Itinerary
    ) -> None:
        await repo.save(itinerary)
        loaded = await repo.get(itinerary.id)
        assert loaded.id == itinerary.id
        assert loaded.budget.total == itinerary.budget.total
        assert len(loaded.days) == len(itinerary.days)
        assert loaded.request == itinerary.request

    @pytest.mark.asyncio
    async def test_fetching_a_missing_id_raises(self, repo: ItineraryRepository) -> None:
        with pytest.raises(ItineraryNotFoundError):
            await repo.get("missing")

    @pytest.mark.asyncio
    async def test_saving_twice_replaces_rather_than_duplicating(
        self, repo: ItineraryRepository, itinerary: Itinerary
    ) -> None:
        await repo.save(itinerary)
        await repo.save(itinerary)
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_summaries_expose_the_listing_columns(
        self, repo: ItineraryRepository, itinerary: Itinerary
    ) -> None:
        await repo.save(itinerary)
        row = (await repo.list_recent())[0]
        assert row["id"] == itinerary.id
        assert row["currency"] == Currency.INR.value
        assert row["total_cost"] == itinerary.budget.total

    @pytest.mark.asyncio
    async def test_delete_removes_the_row(
        self, repo: ItineraryRepository, itinerary: Itinerary
    ) -> None:
        await repo.save(itinerary)
        await repo.delete(itinerary.id)
        assert await repo.count() == 0
        with pytest.raises(ItineraryNotFoundError):
            await repo.delete(itinerary.id)

    @pytest.mark.asyncio
    async def test_list_limit_is_capped(
        self, repo: ItineraryRepository, itinerary: Itinerary
    ) -> None:
        await repo.save(itinerary)
        assert len(await repo.list_recent(limit=10_000)) == 1


class TestCacheKey:
    def test_equivalent_briefs_share_a_key(self, trip: TripRequest) -> None:
        other = TripRequest.model_validate(trip.model_dump(mode="json"))
        assert PlannerService.cache_key(trip) == PlannerService.cache_key(other)

    def test_different_briefs_get_different_keys(self, trip: TripRequest) -> None:
        changed = trip.model_copy(update={"adults": 3})
        assert PlannerService.cache_key(trip) != PlannerService.cache_key(changed)

    def test_key_is_a_hex_digest(self, trip: TripRequest) -> None:
        key = PlannerService.cache_key(trip)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


class TestSettings:
    def test_ai_is_unavailable_without_a_key(self) -> None:
        assert Settings(anthropic_api_key=None).ai_available is False

    def test_force_heuristic_overrides_a_present_key(self) -> None:
        settings = Settings(anthropic_api_key="sk-ant-x", force_heuristic=True)
        assert settings.ai_available is False

    def test_a_key_enables_ai(self) -> None:
        assert Settings(anthropic_api_key="sk-ant-x", force_heuristic=False).ai_available is True

    def test_the_key_is_not_exposed_by_repr(self) -> None:
        """A settings dump must never leak the API key into logs."""
        settings = Settings(anthropic_api_key="sk-ant-secret-value")
        assert "sk-ant-secret-value" not in repr(settings)
        assert settings.api_key() == "sk-ant-secret-value"

    def test_cors_origins_accept_a_comma_separated_string(self) -> None:
        settings = Settings(cors_origins="https://a.example, https://b.example")
        assert settings.cors_origins == ["https://a.example", "https://b.example"]

    def test_settings_are_cached(self) -> None:
        get_settings.cache_clear()
        try:
            assert get_settings() is get_settings()
        finally:
            get_settings.cache_clear()
