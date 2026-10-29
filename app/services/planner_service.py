"""Orchestration: brief in, saved itinerary out.

The pipeline, in order:

1. Price the trip from reference data (:class:`~app.services.budget.BudgetEngine`).
2. Turn the budget into per-unit targets for the prompt.
3. Ask Claude for a draft - or the template planner, if no key is configured,
   if the caller asked for it, or if the model call failed.
4. Reconcile the draft: clamp implausible unit prices, multiply out to party
   totals, sum into buckets, produce a verdict.
5. Assemble, cache and persist the final itinerary.

Step 3 is the only step that can fail in a way the user should not see, and it
is the only step with a fallback.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from app.config import Settings
from app.core.cache import TTLCache
from app.core.errors import AIUnavailableError, PlanGenerationError
from app.core.logging import get_logger
from app.models.draft import ItineraryDraft
from app.models.enums import PlannerMode
from app.models.itinerary import Itinerary, PlannerMeta
from app.models.trip import TripRequest
from app.services.ai.planner import AIPlanner, AIResult
from app.services.budget import BudgetEngine, BudgetEnvelope, ReconciledPlan, TripPricing
from app.services.heuristic import HeuristicPlanner
from app.services.repository import ItineraryRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """A finished plan plus whether it came from the cache."""

    itinerary: Itinerary
    from_cache: bool


class PlannerService:
    """Builds itineraries, with the model when possible and templates when not."""

    def __init__(
        self,
        settings: Settings,
        repository: ItineraryRepository,
        engine: BudgetEngine | None = None,
        ai_planner: AIPlanner | None = None,
        heuristic: HeuristicPlanner | None = None,
        cache: TTLCache[Itinerary] | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._engine = engine or BudgetEngine()
        self._heuristic = heuristic or HeuristicPlanner(self._engine)
        self._cache = (
            cache
            if cache is not None
            else TTLCache[Itinerary](
                ttl_seconds=settings.cache_ttl_seconds, max_entries=settings.cache_max_entries
            )
        )

        self._ai: AIPlanner | None = ai_planner
        if self._ai is None and settings.ai_available:
            try:
                self._ai = AIPlanner(settings)
            except AIUnavailableError:
                # Already logged by the planner; the service simply runs without it.
                self._ai = None

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    @property
    def ai_enabled(self) -> bool:
        return self._ai is not None

    def cache_stats(self) -> dict[str, int | float]:
        return self._cache.stats()

    async def create_plan(self, req: TripRequest, *, use_cache: bool = True) -> PlanOutcome:
        """Plan ``req``, persist the result and return it."""
        key = self.cache_key(req)
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                logger.info("plan_cache_hit", extra={"itinerary_id": cached.id})
                return PlanOutcome(itinerary=cached, from_cache=True)

        started = time.perf_counter()
        baseline = self._engine.baseline(req)
        envelope = self._engine.envelope(req)

        draft, meta = await self._obtain_draft(req, envelope, baseline)
        plan = self._engine.reconcile(req, draft)

        itinerary = self._assemble(req, draft, plan, meta, baseline)
        itinerary.meta.duration_ms = int((time.perf_counter() - started) * 1000)

        await self._repo.save(itinerary)
        self._cache.set(key, itinerary)

        logger.info(
            "plan_created",
            extra={
                "itinerary_id": itinerary.id,
                "mode": itinerary.meta.mode.value,
                "verdict": itinerary.budget.verdict.value,
                "utilisation_pct": itinerary.budget.utilisation_pct,
                "adjustments": len(itinerary.meta.adjustments),
                "duration_ms": itinerary.meta.duration_ms,
            },
        )
        return PlanOutcome(itinerary=itinerary, from_cache=False)

    async def get_plan(self, itinerary_id: str) -> Itinerary:
        """Fetch a stored itinerary."""
        return await self._repo.get(itinerary_id)

    @staticmethod
    def cache_key(req: TripRequest) -> str:
        """Stable hash of the brief, so equivalent requests share a cache entry."""
        canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    async def _obtain_draft(
        self, req: TripRequest, envelope: BudgetEnvelope, baseline: TripPricing
    ) -> tuple[ItineraryDraft, PlannerMeta]:
        """Get a draft from the model, falling back to templates when needed."""
        if self._ai is None:
            return self._heuristic_draft(req, envelope, reason=None)

        try:
            result: AIResult = await self._ai.draft(req, envelope, baseline)
        except AIUnavailableError as exc:
            if not self._settings.fallback_to_heuristic:
                raise PlanGenerationError("Could not generate a plan.", detail=exc.message) from exc
            logger.warning(
                "ai_failed_falling_back",
                # NB: never use "message" as an extra key - it collides with
                # LogRecord's own attribute and makeRecord raises KeyError.
                extra={"reason": exc.code, "detail": exc.message},
            )
            return self._heuristic_draft(req, envelope, reason=exc.message)

        return result.draft, PlannerMeta(
            mode=PlannerMode.AI,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_hit=result.cache_read_tokens > 0,
            duration_ms=result.duration_ms,
        )

    def _heuristic_draft(
        self, req: TripRequest, envelope: BudgetEnvelope, *, reason: str | None
    ) -> tuple[ItineraryDraft, PlannerMeta]:
        """Build a template draft, recording why the model was not used."""
        started = time.perf_counter()
        draft = self._heuristic.draft(req, envelope)
        note = (
            f"AI planning was unavailable ({reason}); this plan was generated from "
            "reference templates and pricing data."
            if reason
            else "Generated from reference templates and pricing data - no AI key is configured."
        )
        return draft, PlannerMeta(
            mode=PlannerMode.HEURISTIC,
            model=self._heuristic.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            adjustments=[note],
        )

    def _assemble(
        self,
        req: TripRequest,
        draft: ItineraryDraft,
        plan: ReconciledPlan,
        meta: PlannerMeta,
        baseline: TripPricing,
    ) -> Itinerary:
        """Fold the draft's prose and the engine's numbers into one object."""
        meta.adjustments = [*meta.adjustments, *plan.adjustments]

        if baseline.distance_estimated:
            meta.adjustments.append(
                "The route is not in the reference dataset, so travel distance and cost "
                "are rough estimates."
            )

        return Itinerary(
            request=req,
            title=draft.title or f"{req.duration_days} days in {req.destination}",
            destination_overview=draft.destination_overview,
            best_time_note=draft.best_time_note,
            food_scene_note=draft.food_scene_note,
            outbound=plan.outbound,
            inbound=plan.inbound,
            days=plan.days,
            budget=plan.budget,
            misc_note=draft.misc_note,
            misc_total=plan.misc_total,
            packing_list=draft.packing_list,
            travel_tips=draft.travel_tips,
            warnings=draft.warnings,
            meta=meta,
        )
