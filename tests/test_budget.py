"""The cost engine: baselines, envelopes, reconciliation and clamping.

These are the tests that matter most. Everything a user is shown about money
comes out of this module, and a plan whose arithmetic is wrong is worse than no
plan at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.draft import ItineraryDraft
from app.models.enums import (
    BudgetVerdict,
    CostCategory,
    Currency,
    Pace,
    StayType,
    TransportMode,
)
from app.models.trip import TripRequest
from app.services.budget import CLAMPS, BudgetEngine
from app.services.heuristic import HeuristicPlanner


@pytest.fixture
def draft(heuristic: HeuristicPlanner, engine: BudgetEngine, trip: TripRequest) -> ItineraryDraft:
    return heuristic.draft(trip, engine.envelope(trip))


class TestBaseline:
    def test_every_category_is_priced(self, engine: BudgetEngine, trip: TripRequest) -> None:
        base = engine.baseline(trip)
        lines = base.as_lines()
        assert set(lines) == set(CostCategory)
        assert all(value > 0 for value in lines.values())
        assert base.total == pytest.approx(sum(lines.values()))

    def test_accommodation_is_charged_per_night_not_per_day(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """A six-day trip has five nights."""
        six = engine.baseline(trip)
        seven = engine.baseline(
            trip.model_copy(update={"end_date": trip.end_date + timedelta(days=1)})
        )
        per_night = seven.accommodation - six.accommodation
        assert six.accommodation == pytest.approx(per_night * trip.nights, rel=0.01)

    def test_more_travellers_cost_more(self, engine: BudgetEngine, trip: TripRequest) -> None:
        solo = engine.baseline(trip.model_copy(update={"adults": 1}))
        pair = engine.baseline(trip)
        assert pair.total > solo.total

    def test_longer_trips_cost_more(self, engine: BudgetEngine, trip: TripRequest) -> None:
        longer = trip.model_copy(update={"end_date": trip.end_date + timedelta(days=4)})
        assert engine.baseline(longer).total > engine.baseline(trip).total

    def test_expensive_destinations_cost_more(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        zurich = engine.baseline(trip.model_copy(update={"destination": "Zurich"}))
        bali = engine.baseline(trip)
        assert zurich.accommodation > bali.accommodation

    def test_currency_conversion_is_applied(self, engine: BudgetEngine, trip: TripRequest) -> None:
        inr = engine.baseline(trip)
        usd = engine.baseline(trip.model_copy(update={"currency": Currency.USD}))
        assert usd.total < inr.total / 50

    def test_international_flag_is_detected(self, engine: BudgetEngine, trip: TripRequest) -> None:
        assert engine.baseline(trip).international is True
        assert (
            engine.baseline(trip.model_copy(update={"destination": "Goa"})).international is False
        )


class TestEnvelope:
    def test_targets_are_positive_for_a_workable_budget(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        env = engine.envelope(trip)
        assert env.feasible
        assert env.stay_per_night > 0
        assert env.food_per_person_per_day > 0
        assert env.activities_per_person_per_day > 0

    def test_a_generous_budget_raises_the_targets(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        lean = engine.envelope(trip.model_copy(update={"budget_total": 140_000}))
        rich = engine.envelope(trip.model_copy(update={"budget_total": 400_000}))
        assert rich.stay_per_night > lean.stay_per_night

    def test_an_impossible_budget_is_flagged(self, engine: BudgetEngine, trip: TripRequest) -> None:
        """Return flights to Bali cost far more than this."""
        env = engine.envelope(trip.model_copy(update={"budget_total": 1_000}))
        assert env.feasible is False
        assert env.notes

    def test_envelope_never_exceeds_the_budget(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        env = engine.envelope(trip)
        committed = (
            env.transport_allowance
            + env.misc_allowance
            + env.stay_per_night * trip.nights
            + (
                env.food_per_person_per_day
                + env.activities_per_person_per_day
                + env.local_transport_per_person_per_day
            )
            * trip.travellers
            * trip.duration_days
        )
        assert committed <= trip.budget_total * 1.02


class TestReconciliation:
    def test_totals_are_the_sum_of_their_parts(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        plan = engine.reconcile(trip, draft)
        assert plan.budget.total == pytest.approx(
            sum(line.amount for line in plan.budget.lines), abs=1.0
        )

    def test_day_totals_sum_to_their_lines(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        for day in engine.reconcile(trip, draft).days:
            expected = (
                sum(a.cost_total for a in day.activities)
                + sum(m.cost_total for m in day.meals)
                + (day.stay.cost_total if day.stay else 0)
                + day.local_transport_total
            )
            assert day.day_total == pytest.approx(expected, abs=1.0)

    def test_shares_add_up_to_a_hundred_percent(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        plan = engine.reconcile(trip, draft)
        assert sum(line.share_pct for line in plan.budget.lines) == pytest.approx(100, abs=0.5)

    def test_per_person_figures_are_consistent(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        budget = engine.reconcile(trip, draft).budget
        assert budget.per_person == pytest.approx(budget.total / trip.travellers, abs=1.0)
        assert budget.per_person_per_day == pytest.approx(
            budget.total / (trip.travellers * trip.duration_days), abs=1.0
        )

    def test_unit_prices_are_multiplied_by_party_size(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        for day in engine.reconcile(trip, draft).days:
            for activity in day.activities:
                assert activity.cost_total == pytest.approx(
                    activity.cost_per_person * trip.travellers, abs=1.0
                )

    def test_the_departure_night_is_not_charged(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        """Six days means five hotel nights, not six."""
        plan = engine.reconcile(trip, draft)
        charged = [d for d in plan.days if d.stay is not None and d.stay.charged]
        assert len(charged) == trip.nights
        assert plan.days[-1].stay is not None
        assert plan.days[-1].stay.charged is False
        assert plan.days[-1].stay.cost_total == 0

    def test_days_are_dated_consecutively_from_departure(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        plan = engine.reconcile(trip, draft)
        assert len(plan.days) == trip.duration_days
        for index, day in enumerate(plan.days):
            assert day.day_number == index + 1
            assert day.day_date == trip.start_date + timedelta(days=index)

    def test_extra_days_beyond_the_trip_are_dropped(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        padded = draft.model_copy(deep=True)
        padded.days.append(padded.days[0].model_copy(update={"day_number": 99}))
        plan = engine.reconcile(trip, padded)
        assert len(plan.days) == trip.duration_days
        assert any("Dropped day 99" in note for note in plan.adjustments)


class TestClamping:
    """The guard rail that makes model-supplied numbers safe to show."""

    def test_an_absurdly_high_stay_is_corrected(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        bad = draft.model_copy(deep=True)
        bad.days[0].stay.estimated_cost_per_night_total = 5_000_000
        plan = engine.reconcile(trip, bad)
        assert plan.days[0].stay is not None
        assert plan.days[0].stay.cost_total < 100_000
        assert any("implausibly high" in note for note in plan.adjustments)

    def test_an_absurdly_low_flight_is_corrected(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        bad = draft.model_copy(deep=True)
        bad.outbound.estimated_cost_per_person = 1.0
        plan = engine.reconcile(trip, bad)
        assert plan.outbound.cost_per_person > 100
        assert any("implausibly low" in note for note in plan.adjustments)

    def test_plausible_prices_are_left_alone(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        plan = engine.reconcile(trip, draft)
        assert not any("implausibly" in note for note in plan.adjustments)

    def test_a_hallucinated_plan_stays_within_an_order_of_magnitude(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        """Every unit price poisoned at once must still produce a sane total."""
        bad = draft.model_copy(deep=True)
        bad.misc_cost_per_person = 10_000_000
        bad.outbound.estimated_cost_per_person = 9_000_000
        bad.inbound.estimated_cost_per_person = 0.0
        for day in bad.days:
            day.stay.estimated_cost_per_night_total = 8_000_000
            day.local_transport_cost_per_person = 500_000
            for activity in day.activities:
                activity.estimated_cost_per_person = 2_000_000
            for meal in day.meals:
                meal.estimated_cost_per_person = 400_000

        plan = engine.reconcile(trip, bad)
        clean_total = engine.reconcile(trip, draft).budget.total
        assert plan.budget.total < clean_total * 10
        assert len(plan.adjustments) > 5

    def test_negative_prices_are_floored(
        self, engine: BudgetEngine, trip: TripRequest, draft: ItineraryDraft
    ) -> None:
        bad = draft.model_copy(deep=True)
        bad.days[0].activities[0].estimated_cost_per_person = 0.0
        plan = engine.reconcile(trip, bad)
        assert all(a.cost_total >= 0 for d in plan.days for a in d.activities)

    def test_clamp_bounds_are_ordered(self) -> None:
        for kind, (low, high) in CLAMPS.items():
            assert low <= high, kind


class TestVerdicts:
    @pytest.mark.parametrize(
        ("budget_multiplier", "expected"),
        [
            (3.0, BudgetVerdict.UNDER_BUDGET),
            (0.4, BudgetVerdict.OVER_BUDGET),
        ],
    )
    def test_verdict_reflects_utilisation(
        self,
        engine: BudgetEngine,
        trip: TripRequest,
        budget_multiplier: float,
        expected: BudgetVerdict,
    ) -> None:
        base = engine.baseline(trip)
        adjusted = trip.model_copy(update={"budget_total": base.total * budget_multiplier})
        summary = engine.summarise(adjusted, base.as_lines(), base)
        assert summary.verdict is expected

    def test_within_budget_property_tracks_the_verdict(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        base = engine.baseline(trip)
        over = engine.summarise(
            trip.model_copy(update={"budget_total": base.total * 0.5}), base.as_lines(), base
        )
        under = engine.summarise(
            trip.model_copy(update={"budget_total": base.total * 2}), base.as_lines(), base
        )
        assert over.within_budget is False
        assert under.within_budget is True

    def test_difference_is_negative_when_over_budget(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        base = engine.baseline(trip)
        tight = trip.model_copy(update={"budget_total": base.total * 0.5})
        assert engine.summarise(tight, base.as_lines(), base).difference < 0

    def test_line_lookup_returns_a_zero_line_when_missing(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        summary = engine.summarise(trip, {CostCategory.FOOD: 100.0})
        assert summary.line_for(CostCategory.FOOD).amount == 100.0
        assert summary.line_for(CostCategory.MISC).amount == 0.0


class TestAdvice:
    def test_over_budget_advice_is_quantified(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        base = engine.baseline(trip)
        tight = trip.model_copy(update={"budget_total": base.total * 0.5})
        advice = engine.summarise(tight, base.as_lines(), base).advice
        assert advice
        assert any("saves about" in line for line in advice)

    def test_advice_never_suggests_an_impossible_mode(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        """There is no train from Mumbai to Bali."""
        base = engine.baseline(trip)
        tight = trip.model_copy(update={"budget_total": base.total * 0.4})
        advice = " ".join(engine.summarise(tight, base.as_lines(), base).advice)
        assert "train" not in advice.lower()

    def test_advice_suggests_a_train_when_one_is_realistic(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        domestic = trip.model_copy(
            update={"destination": "Goa", "transport_modes": [TransportMode.FLIGHT]}
        )
        base = engine.baseline(domestic)
        tight = domestic.model_copy(update={"budget_total": base.total * 0.4})
        advice = " ".join(engine.summarise(tight, base.as_lines(), base).advice)
        assert "train" in advice.lower()

    def test_under_budget_advice_mentions_headroom(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        base = engine.baseline(trip)
        rich = trip.model_copy(update={"budget_total": base.total * 3})
        advice = engine.summarise(rich, base.as_lines(), base).advice
        assert any("headroom" in line for line in advice)

    def test_a_hostel_traveller_is_not_told_to_downgrade_further(
        self, engine: BudgetEngine, trip: TripRequest
    ) -> None:
        hostel = trip.model_copy(
            update={"stay_type": StayType.HOSTEL, "pace": Pace.RELAXED, "budget_total": 1_000}
        )
        base = engine.baseline(hostel)
        advice = " ".join(engine.summarise(hostel, base.as_lines(), base).advice)
        assert "hostel to hostel" not in advice
