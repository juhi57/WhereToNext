"""The template planner.

It has to produce a *usable* plan, not just a well-formed one: the right number
of days, times that run forwards, meals that respect the diet, and the same
output every time so the rest of the suite can rely on it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.enums import FoodPreference, Interest, Pace, StayType, TransportMode
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.heuristic import ACTIVITIES_PER_DAY, HeuristicPlanner


def plan_for(heuristic: HeuristicPlanner, engine: BudgetEngine, req: TripRequest):
    return heuristic.draft(req, engine.envelope(req))


class TestShape:
    def test_covers_every_day_exactly_once(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        draft = plan_for(heuristic, engine, trip)
        assert len(draft.days) == trip.duration_days
        assert [d.day_number for d in draft.days] == list(range(1, trip.duration_days + 1))

    @pytest.mark.parametrize("days", [1, 2, 3, 7, 14, 30])
    def test_handles_every_trip_length(
        self,
        heuristic: HeuristicPlanner,
        engine: BudgetEngine,
        start_date: date,
        days: int,
    ) -> None:
        req = TripRequest(
            origin="Mumbai",
            destination="Goa",
            start_date=start_date,
            end_date=start_date + timedelta(days=days - 1),
            budget_total=20_000 * days,
        )
        draft = plan_for(heuristic, engine, req)
        assert len(draft.days) == days
        assert all(day.activities for day in draft.days)

    def test_every_day_has_a_stay_and_meals(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        for day in plan_for(heuristic, engine, trip).days:
            assert day.stay is not None
            assert day.meals

    def test_outbound_and_return_mirror_each_other(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        draft = plan_for(heuristic, engine, trip)
        assert draft.outbound.from_place == trip.origin
        assert draft.inbound.to_place == trip.origin
        assert draft.outbound.to_place == draft.inbound.from_place

    def test_chosen_transport_mode_is_honoured(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        by_train = trip.model_copy(
            update={"destination": "Goa", "transport_modes": [TransportMode.TRAIN]}
        )
        draft = plan_for(heuristic, engine, by_train)
        assert draft.outbound.mode is TransportMode.TRAIN


class TestScheduling:
    def test_times_run_forwards_on_every_day(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        for day in plan_for(heuristic, engine, trip).days:
            times = [a.start_time for a in day.activities]
            assert times == sorted(times), f"day {day.day_number} is out of order"

    def test_evening_activities_are_scheduled_in_the_evening(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """A sunset viewpoint at 09:00 destroys confidence in the whole plan."""
        evening_words = ("sunset", "sundowner", "night", "evening", "dusk")
        found = False
        for day in plan_for(heuristic, engine, trip).days:
            for activity in day.activities:
                if any(word in activity.title.lower() for word in evening_words):
                    found = True
                    hour = int(activity.start_time.split(":")[0])
                    assert hour >= 16, f"{activity.title} at {activity.start_time}"
        assert found, "fixture should contain at least one evening activity"

    def test_arrival_day_starts_in_the_afternoon(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        first = plan_for(heuristic, engine, trip).days[0]
        assert "arriv" in first.activities[0].title.lower()
        assert int(first.activities[0].start_time.split(":")[0]) >= 12

    def test_departure_day_ends_with_checkout_before_evening(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        last = plan_for(heuristic, engine, trip).days[-1]
        checkout = last.activities[-1]
        assert "check out" in checkout.title.lower()
        assert int(checkout.start_time.split(":")[0]) < 16

    def test_pace_controls_activity_density(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        def middle_day_count(pace: Pace) -> int:
            draft = plan_for(heuristic, engine, trip.model_copy(update={"pace": pace}))
            return len(draft.days[2].activities)

        assert middle_day_count(Pace.RELAXED) == ACTIVITIES_PER_DAY[Pace.RELAXED]
        assert middle_day_count(Pace.PACKED) == ACTIVITIES_PER_DAY[Pace.PACKED]
        assert middle_day_count(Pace.RELAXED) < middle_day_count(Pace.PACKED)


class TestContent:
    def test_dietary_preference_reaches_every_meal(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        jain = trip.model_copy(update={"food_preference": FoodPreference.JAIN})
        for day in plan_for(heuristic, engine, jain).days:
            for meal in day.meals:
                assert meal.dietary_note
                assert "jain" in f"{meal.cuisine} {meal.suggestion}".lower()

    def test_interests_steer_the_activities(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        wildlife = trip.model_copy(update={"interests": [Interest.WILDLIFE]})
        titles = " ".join(
            a.title.lower()
            for d in plan_for(heuristic, engine, wildlife).days
            for a in d.activities
        )
        assert any(word in titles for word in ("safari", "birding", "conservation"))

    def test_no_interests_still_yields_a_plan(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        draft = plan_for(heuristic, engine, trip.model_copy(update={"interests": []}))
        assert all(day.activities for day in draft.days)

    def test_international_trips_warn_about_documents(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        draft = plan_for(heuristic, engine, trip)
        packing = " ".join(draft.packing_list).lower()
        assert "passport" in packing
        assert any("visa" in w.lower() for w in draft.warnings)

    def test_domestic_trips_do_not_mention_visas(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        domestic = trip.model_copy(update={"destination": "Goa"})
        draft = plan_for(heuristic, engine, domestic)
        assert "passport" not in " ".join(draft.packing_list).lower()

    def test_hostel_stays_get_hostel_specific_packing(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        draft = plan_for(heuristic, engine, trip.model_copy(update={"stay_type": StayType.HOSTEL}))
        assert any("padlock" in item.lower() for item in draft.packing_list)

    def test_accessibility_needs_are_acknowledged(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        needs = trip.model_copy(update={"accessibility_needs": "wheelchair user, no stairs"})
        draft = plan_for(heuristic, engine, needs)
        joined = " ".join(draft.travel_tips + draft.packing_list).lower()
        assert "access" in joined

    def test_an_implausible_mode_is_flagged(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """Asking for a train to Bali should produce a warning, not silence."""
        by_train = trip.model_copy(update={"transport_modes": [TransportMode.TRAIN]})
        warnings = " ".join(plan_for(heuristic, engine, by_train).warnings).lower()
        assert "may not be practical" in warnings

    def test_estimates_are_declared_as_estimates(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        assert any("estimate" in w.lower() for w in plan_for(heuristic, engine, trip).warnings)


class TestDeterminism:
    def test_identical_requests_produce_identical_plans(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        first = plan_for(heuristic, engine, trip)
        second = plan_for(heuristic, engine, trip)
        assert first.model_dump_json() == second.model_dump_json()

    def test_different_requests_produce_different_plans(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        other = trip.model_copy(update={"interests": [Interest.HISTORY, Interest.ART]})
        assert (
            plan_for(heuristic, engine, trip).model_dump_json()
            != plan_for(heuristic, engine, other).model_dump_json()
        )

    def test_a_fresh_planner_instance_agrees(self, engine: BudgetEngine, trip: TripRequest) -> None:
        """Determinism must not depend on instance state."""
        a = HeuristicPlanner(engine).draft(trip, engine.envelope(trip))
        b = HeuristicPlanner(engine).draft(trip, engine.envelope(trip))
        assert a.model_dump_json() == b.model_dump_json()


class TestBudgetFit:
    def test_the_plan_lands_near_the_budget(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """The envelope exists so that template plans do not wildly overshoot."""
        plan = engine.reconcile(trip, plan_for(heuristic, engine, trip))
        assert 70 <= plan.budget.utilisation_pct <= 102

    @pytest.mark.parametrize("budget", [120_000, 250_000, 500_000])
    def test_it_adapts_to_the_budget_given(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest, budget: float
    ) -> None:
        req = trip.model_copy(update={"budget_total": budget})
        plan = engine.reconcile(req, plan_for(heuristic, engine, req))
        assert plan.budget.total <= budget * 1.05

    def test_an_impossible_budget_still_produces_a_plan(
        self, heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """Better an honest over-budget plan than an error page."""
        broke = trip.model_copy(update={"budget_total": 500})
        plan = engine.reconcile(broke, plan_for(heuristic, engine, broke))
        assert len(plan.days) == broke.duration_days
        assert plan.budget.verdict.value == "over_budget"
