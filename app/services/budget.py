"""The cost engine: the only module allowed to produce a total.

Three jobs, in the order the planner calls them:

1. :meth:`BudgetEngine.baseline` - price the trip from reference data alone. No
   model involved. This is the yardstick everything else is measured against and
   the basis of the heuristic planner.
2. :meth:`BudgetEngine.envelope` - turn the user's budget into concrete per-night
   and per-person-per-day targets that get written into the prompt, so the model
   aims at the right price bracket instead of guessing.
3. :meth:`BudgetEngine.reconcile` - take the model's draft, clamp any unit price
   that is wildly out of line with the reference data, multiply unit prices out
   to party totals, and sum them into the six spend buckets.

Step 3 is the reason the output can be trusted: a language model that invents a
"₹400 per night beach resort" has that line corrected and the correction is
reported in ``PlannerMeta.adjustments`` rather than silently shipped.

All internal maths happens in the **user's chosen currency**. Reference rates are
stored in INR and converted once, at the boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

from app.core.money import round_money
from app.models.draft import (
    DraftActivity,
    DraftMeal,
    DraftStay,
    DraftTransportLeg,
    ItineraryDraft,
)
from app.models.enums import (
    BudgetVerdict,
    CostCategory,
    Currency,
    MealType,
    Pace,
    StayType,
    TransportMode,
)
from app.models.itinerary import (
    Activity,
    BudgetBreakdown,
    BudgetLine,
    ItineraryDay,
    Meal,
    StayPlan,
    TransportLeg,
)
from app.models.trip import TripRequest
from app.services import cost_data as cd

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Utilisation (plan total as a percentage of budget) that separates verdicts.
UNDER_BUDGET_BELOW_PCT = 80.0
ON_BUDGET_BELOW_PCT = 97.0
NEAR_LIMIT_BELOW_PCT = 100.5

#: Multiples of the reference unit price outside which a model-supplied figure is
#: treated as a mistake and replaced. Generous on the high side, because a single
#: hot-air-balloon ride legitimately costs many times an average activity.
CLAMPS: dict[str, tuple[float, float]] = {
    "stay_night": (0.20, 4.0),
    "meal": (0.15, 6.0),
    "activity": (0.0, 8.0),
    "transport_leg": (0.25, 3.5),
    "local_transport_day": (0.0, 6.0),
    "misc": (0.0, 5.0),
}

#: Fraction of the budget held back as a safety margin when building the envelope.
ENVELOPE_RESERVE = 0.06


@dataclass(frozen=True, slots=True)
class TripPricing:
    """A deterministic, reference-data-only estimate of what a trip costs."""

    currency: Currency
    distance_km: float
    distance_estimated: bool
    tier: str
    multiplier: float
    international: bool

    transport: float
    accommodation: float
    food: float
    activities: float
    local_transport: float
    misc: float

    @property
    def total(self) -> float:
        return (
            self.transport
            + self.accommodation
            + self.food
            + self.activities
            + self.local_transport
            + self.misc
        )

    def as_lines(self) -> dict[CostCategory, float]:
        return {
            CostCategory.TRANSPORT: self.transport,
            CostCategory.ACCOMMODATION: self.accommodation,
            CostCategory.FOOD: self.food,
            CostCategory.ACTIVITIES: self.activities,
            CostCategory.LOCAL_TRANSPORT: self.local_transport,
            CostCategory.MISC: self.misc,
        }


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    """Per-unit spending targets derived from the user's budget.

    These are what make the model's numbers land near the budget: instead of
    "stay under ₹1,80,000", the prompt can say "₹6,400 per night, ₹1,900 per
    person per day on food".
    """

    currency: Currency
    budget_total: float
    reserve: float
    transport_allowance: float
    misc_allowance: float
    stay_per_night: float
    food_per_person_per_day: float
    activities_per_person_per_day: float
    local_transport_per_person_per_day: float
    feasible: bool
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class ReconciledPlan:
    """Output of :meth:`BudgetEngine.reconcile`."""

    outbound: TransportLeg
    inbound: TransportLeg
    days: list[ItineraryDay]
    misc_total: float
    budget: BudgetBreakdown
    adjustments: list[str] = field(default_factory=list)


class BudgetEngine:
    """Prices trips and reconciles model drafts against reference data."""

    def __init__(self, book: cd.PlaceBook | None = None) -> None:
        self._book = book or cd.place_book()

    # ----------------------------------------------------------------- #
    # 1. Reference-only baseline
    # ----------------------------------------------------------------- #

    def baseline(self, req: TripRequest) -> TripPricing:
        """Price ``req`` from reference data alone, in the request's currency."""
        tier, multiplier = self._book.tier_for_text(req.destination)
        distance_km, estimated = cd.distance_between(req.origin, req.destination)
        international = cd.is_international(req.origin, req.destination)

        people, days, nights = req.travellers, req.duration_days, req.nights

        transport_inr = cd.transport_cost(
            req.primary_transport(), distance_km, people, round_trip=True
        )
        accommodation_inr = cd.stay_cost_per_night(req.stay_type, people, multiplier) * nights
        food_inr = cd.meal_cost_per_person_per_day(multiplier, req.stay_type) * people * days
        activities_inr = (
            cd.activity_cost_per_person_per_day(multiplier, req.pace, req.interests) * people * days
        )
        local_inr = cd.local_transport_per_person_per_day(multiplier) * people * days
        misc_inr = cd.misc_per_person(multiplier, international) * people

        def conv(amount_inr: float) -> float:
            return round_money(cd.from_inr(amount_inr, req.currency), req.currency)

        return TripPricing(
            currency=req.currency,
            distance_km=round(distance_km, 1),
            distance_estimated=estimated,
            tier=tier,
            multiplier=multiplier,
            international=international,
            transport=conv(transport_inr),
            accommodation=conv(accommodation_inr),
            food=conv(food_inr),
            activities=conv(activities_inr),
            local_transport=conv(local_inr),
            misc=conv(misc_inr),
        )

    # ----------------------------------------------------------------- #
    # 2. Budget envelope for the prompt
    # ----------------------------------------------------------------- #

    def envelope(self, req: TripRequest) -> BudgetEnvelope:
        """Split the user's budget into per-unit targets for the model.

        Transport and miscellaneous costs are carved out first because they are
        the least negotiable - you cannot shop around the distance to Bali. What
        remains is divided between accommodation, food, activities and local
        transport in the same proportion the baseline suggests, so an
        adventure-heavy trip keeps its adventure budget.
        """
        base = self.baseline(req)
        people, days, nights = req.travellers, req.duration_days, req.nights
        notes: list[str] = []

        reserve = round_money(req.budget_total * ENVELOPE_RESERVE, req.currency)
        spendable = req.budget_total - reserve

        transport_allowance = min(base.transport, spendable)
        if base.transport > spendable:
            notes.append(
                "Return travel alone consumes the entire budget; consider a nearer "
                "destination, a cheaper mode, or a higher budget."
            )
        remaining = spendable - transport_allowance

        misc_allowance = min(base.misc, remaining)
        remaining -= misc_allowance

        flexible = {
            CostCategory.ACCOMMODATION: base.accommodation,
            CostCategory.FOOD: base.food,
            CostCategory.ACTIVITIES: base.activities,
            CostCategory.LOCAL_TRANSPORT: base.local_transport,
        }
        flexible_baseline = sum(flexible.values())

        if flexible_baseline <= 0 or remaining <= 0:
            feasible = remaining > 0
            if not feasible:
                notes.append("Nothing is left for stay, food and activities after travel costs.")
            return BudgetEnvelope(
                currency=req.currency,
                budget_total=req.budget_total,
                reserve=reserve,
                transport_allowance=round_money(transport_allowance, req.currency),
                misc_allowance=round_money(misc_allowance, req.currency),
                stay_per_night=0.0,
                food_per_person_per_day=0.0,
                activities_per_person_per_day=0.0,
                local_transport_per_person_per_day=0.0,
                feasible=feasible,
                notes=tuple(notes),
            )

        scale = remaining / flexible_baseline
        if scale < 0.6:
            notes.append(
                "The budget is tight for this trip: expect simpler stays and fewer paid "
                "activities than the chosen preferences imply."
            )
        elif scale > 1.8:
            notes.append(
                "The budget is generous for this trip: there is room to upgrade stays or "
                "add premium experiences."
            )

        def per(amount: float, divisor: int) -> float:
            return round_money(amount * scale / divisor, req.currency) if divisor else 0.0

        return BudgetEnvelope(
            currency=req.currency,
            budget_total=req.budget_total,
            reserve=reserve,
            transport_allowance=round_money(transport_allowance, req.currency),
            misc_allowance=round_money(misc_allowance, req.currency),
            stay_per_night=per(flexible[CostCategory.ACCOMMODATION], max(nights, 1)),
            food_per_person_per_day=per(flexible[CostCategory.FOOD], people * days),
            activities_per_person_per_day=per(flexible[CostCategory.ACTIVITIES], people * days),
            local_transport_per_person_per_day=per(
                flexible[CostCategory.LOCAL_TRANSPORT], people * days
            ),
            feasible=True,
            notes=tuple(notes),
        )

    # ----------------------------------------------------------------- #
    # 3. Reconcile a model draft
    # ----------------------------------------------------------------- #

    def reconcile(self, req: TripRequest, draft: ItineraryDraft) -> ReconciledPlan:
        """Cost a model draft, correcting unit prices that defy reference data."""
        base = self.baseline(req)
        _tier, multiplier = self._book.tier_for_text(req.destination)
        people = req.travellers
        adjustments: list[str] = []

        ref = _ReferenceUnits.build(req, multiplier, base)

        outbound = self._cost_leg(draft.outbound, people, ref, adjustments, "Outbound")
        inbound = self._cost_leg(draft.inbound, people, ref, adjustments, "Return")

        days = self._cost_days(req, draft, people, ref, adjustments)
        self._mark_uncharged_final_night(days, req.nights, adjustments)

        misc_pp = _clamp(
            draft.misc_cost_per_person,
            ref.misc_per_person,
            "misc",
            adjustments,
            "Miscellaneous costs",
            req.currency,
        )
        misc_total = round_money(misc_pp * people, req.currency)

        totals = self._sum_categories(outbound, inbound, days, misc_total, req.currency)
        budget = self.summarise(req, totals, base)

        return ReconciledPlan(
            outbound=outbound,
            inbound=inbound,
            days=days,
            misc_total=misc_total,
            budget=budget,
            adjustments=adjustments,
        )

    # ----------------------------------------------------------------- #
    # Reconciliation helpers
    # ----------------------------------------------------------------- #

    def _cost_leg(
        self,
        leg: DraftTransportLeg,
        people: int,
        ref: _ReferenceUnits,
        adjustments: list[str],
        label: str,
    ) -> TransportLeg:
        """Convert a draft transport leg into a costed one."""
        rate = cd.TRANSPORT_RATES.get(leg.mode, cd.TRANSPORT_RATES[TransportMode.FLIGHT])

        per_person = _clamp(
            leg.estimated_cost_per_person,
            ref.transport_leg_for(leg.mode),
            "transport_leg",
            adjustments,
            f"{label} {leg.mode.value} fare",
            ref.currency,
        )
        # A hired car is billed per vehicle; everything else scales with heads.
        total = per_person if not rate.per_person else per_person * people
        return TransportLeg(
            mode=leg.mode,
            from_place=leg.from_place,
            to_place=leg.to_place,
            depart_hint=leg.depart_hint,
            duration_hint=leg.duration_hint,
            description=leg.description,
            booking_tip=leg.booking_tip,
            cost_per_person=round_money(per_person if rate.per_person else 0.0, ref.currency),
            cost_total=round_money(total, ref.currency),
        )

    def _cost_days(
        self,
        req: TripRequest,
        draft: ItineraryDraft,
        people: int,
        ref: _ReferenceUnits,
        adjustments: list[str],
    ) -> list[ItineraryDay]:
        """Convert draft days into costed days, in calendar order."""
        currency = req.currency
        ordered = sorted(draft.days, key=lambda d: d.day_number)
        days: list[ItineraryDay] = []

        for index, draft_day in enumerate(ordered):
            if index >= req.duration_days:
                adjustments.append(
                    f"Dropped day {draft_day.day_number}: the trip is only "
                    f"{req.duration_days} days long."
                )
                continue

            activities = [
                self._cost_activity(a, people, ref, adjustments, index + 1)
                for a in draft_day.activities
            ]
            meals = [
                self._cost_meal(m, people, ref, adjustments, index + 1) for m in draft_day.meals
            ]
            stay = self._cost_stay(draft_day.stay, req, ref, adjustments, index + 1)

            local_pp = _clamp(
                draft_day.local_transport_cost_per_person,
                ref.local_transport_per_day,
                "local_transport_day",
                adjustments,
                f"Day {index + 1} local transport",
                currency,
            )
            local_total = round_money(local_pp * people, currency)

            day_total = round_money(
                sum(a.cost_total for a in activities)
                + sum(m.cost_total for m in meals)
                + (stay.cost_total if stay else 0.0)
                + local_total,
                currency,
            )

            days.append(
                ItineraryDay(
                    day_number=index + 1,
                    day_date=req.start_date + timedelta(days=index),
                    title=draft_day.title,
                    theme=draft_day.theme,
                    activities=activities,
                    meals=meals,
                    stay=stay,
                    local_transport_note=draft_day.local_transport_note,
                    local_transport_total=local_total,
                    day_total=day_total,
                )
            )

        if len(days) < req.duration_days:
            adjustments.append(
                f"The plan covers {len(days)} of {req.duration_days} days; "
                "the remaining days are left unscheduled."
            )
        return days

    def _cost_activity(
        self,
        draft: DraftActivity,
        people: int,
        ref: _ReferenceUnits,
        adjustments: list[str],
        day: int,
    ) -> Activity:
        per_person = _clamp(
            draft.estimated_cost_per_person,
            ref.activity_per_day,
            "activity",
            adjustments,
            f"Day {day} activity '{draft.title}'",
            ref.currency,
        )
        return Activity(
            start_time=draft.start_time,
            title=draft.title,
            description=draft.description,
            category=draft.category,
            duration_minutes=draft.duration_minutes,
            location=draft.location,
            booking_tip=draft.booking_tip,
            cost_per_person=round_money(per_person, ref.currency),
            cost_total=round_money(per_person * people, ref.currency),
        )

    def _cost_meal(
        self,
        draft: DraftMeal,
        people: int,
        ref: _ReferenceUnits,
        adjustments: list[str],
        day: int,
    ) -> Meal:
        meal_type = draft.meal_type
        per_person = _clamp(
            draft.estimated_cost_per_person,
            ref.meal_for(meal_type),
            "meal",
            adjustments,
            f"Day {day} {meal_type.value}",
            ref.currency,
        )
        return Meal(
            meal_type=meal_type,
            suggestion=draft.suggestion,
            cuisine=draft.cuisine,
            location=draft.location,
            dietary_note=draft.dietary_note,
            cost_per_person=round_money(per_person, ref.currency),
            cost_total=round_money(per_person * people, ref.currency),
        )

    def _cost_stay(
        self,
        draft: DraftStay,
        req: TripRequest,
        ref: _ReferenceUnits,
        adjustments: list[str],
        day: int,
    ) -> StayPlan:
        stay_type = draft.stay_type
        total = _clamp(
            draft.estimated_cost_per_night_total,
            ref.stay_night_for(stay_type, req.travellers),
            "stay_night",
            adjustments,
            f"Day {day} stay '{draft.name}'",
            req.currency,
        )
        return StayPlan(
            name=draft.name,
            stay_type=stay_type,
            area=draft.area,
            why=draft.why,
            booking_tip=draft.booking_tip,
            cost_per_person=round_money(total / max(req.travellers, 1), req.currency),
            cost_total=round_money(total, req.currency),
            charged=True,
        )

    @staticmethod
    def _mark_uncharged_final_night(
        days: list[ItineraryDay], nights: int, adjustments: list[str]
    ) -> None:
        """Stop charging for a bed on the day the traveller flies home.

        A 6-day trip has 5 nights. The model still describes a stay on day 6
        (checkout, luggage storage), so the entry is kept for context but its
        cost is zeroed and the day total re-derived.
        """
        for day in days[nights:]:
            if day.stay is None:
                continue
            removed = day.stay.cost_total
            day.stay = day.stay.model_copy(
                update={"charged": False, "cost_total": 0.0, "cost_per_person": 0.0}
            )
            day.day_total = max(day.day_total - removed, 0.0)
            if removed > 0:
                adjustments.append(
                    f"Day {day.day_number} is the departure day: removed one night of "
                    "accommodation that would not actually be paid for."
                )

    @staticmethod
    def _sum_categories(
        outbound: TransportLeg,
        inbound: TransportLeg,
        days: list[ItineraryDay],
        misc_total: float,
        currency: Currency,
    ) -> dict[CostCategory, float]:
        """Fold every costed line into the six spend buckets."""
        accommodation = sum(d.stay.cost_total for d in days if d.stay is not None)
        food = sum(m.cost_total for d in days for m in d.meals)
        activities = sum(a.cost_total for d in days for a in d.activities)
        local = sum(d.local_transport_total for d in days)

        return {
            CostCategory.TRANSPORT: round_money(outbound.cost_total + inbound.cost_total, currency),
            CostCategory.ACCOMMODATION: round_money(accommodation, currency),
            CostCategory.FOOD: round_money(food, currency),
            CostCategory.ACTIVITIES: round_money(activities, currency),
            CostCategory.LOCAL_TRANSPORT: round_money(local, currency),
            CostCategory.MISC: round_money(misc_total, currency),
        }

    # ----------------------------------------------------------------- #
    # Verdict and advice
    # ----------------------------------------------------------------- #

    def summarise(
        self,
        req: TripRequest,
        totals: dict[CostCategory, float],
        base: TripPricing | None = None,
    ) -> BudgetBreakdown:
        """Build the budget breakdown, verdict and savings advice."""
        currency = req.currency
        total = round_money(sum(totals.values()), currency)
        notes = _CATEGORY_NOTES

        lines = [
            BudgetLine(
                category=category,
                amount=totals.get(category, 0.0),
                share_pct=round((totals.get(category, 0.0) / total) * 100, 1) if total else 0.0,
                note=notes.get(category, ""),
            )
            for category in CostCategory
        ]

        utilisation = (total / req.budget_total * 100) if req.budget_total else 0.0
        verdict = _verdict_for(utilisation)
        advice = self._advice(req, total, verdict, base or self.baseline(req))

        return BudgetBreakdown(
            currency=currency,
            lines=lines,
            total=total,
            per_person=round_money(total / max(req.travellers, 1), currency),
            per_person_per_day=round_money(
                total / max(req.travellers * req.duration_days, 1), currency
            ),
            budget_total=round_money(req.budget_total, currency),
            difference=round_money(abs(req.budget_total - total), currency)
            * (1 if req.budget_total >= total else -1),
            utilisation_pct=round(utilisation, 1),
            verdict=verdict,
            advice=advice,
        )

    def _advice(
        self,
        req: TripRequest,
        total: float,
        verdict: BudgetVerdict,
        base: TripPricing,
    ) -> list[str]:
        """Concrete, quantified next steps - not generic travel platitudes."""
        currency = req.currency
        if verdict is BudgetVerdict.UNDER_BUDGET:
            spare = round_money(req.budget_total - total, currency)
            return [
                f"You have roughly {_fmt(spare, currency)} of headroom - enough to upgrade "
                "the stay tier or add a premium experience.",
                "Consider booking refundable fares with the spare budget rather than "
                "spending all of it.",
            ]
        if verdict in (BudgetVerdict.ON_BUDGET, BudgetVerdict.NEAR_LIMIT):
            return [
                "The plan fits, but with little slack - keep "
                f"{_fmt(round_money(total * 0.1, currency), currency)} aside for surge "
                "pricing and last-minute changes.",
            ]

        # Over budget: quantify each lever against the reference model.
        over = round_money(total - req.budget_total, currency)
        tips = [f"The plan is about {_fmt(over, currency)} over budget. Options:"]
        tips.extend(self._savings_levers(req, base))
        return tips

    def _savings_levers(self, req: TripRequest, base: TripPricing) -> list[str]:
        """Rank the four ways to cut cost by how much each actually saves."""
        currency = req.currency
        _, multiplier = self._book.tier_for_text(req.destination)
        levers: list[tuple[float, str]] = []

        cheaper_stay = _CHEAPER_STAY.get(req.stay_type)
        if cheaper_stay is not None and req.nights:
            now = cd.stay_cost_per_night(req.stay_type, req.travellers, multiplier)
            then = cd.stay_cost_per_night(cheaper_stay, req.travellers, multiplier)
            saving = cd.from_inr((now - then) * req.nights, currency)
            if saving > 0:
                levers.append(
                    (
                        saving,
                        f"Switch the stay from {_label(req.stay_type)} to "
                        f"{_label(cheaper_stay)}: saves about "
                        f"{_fmt(round_money(saving, currency), currency)}.",
                    )
                )

        mode = req.primary_transport()
        cheaper_mode = _CHEAPER_MODE.get(mode)
        if cheaper_mode is not None and cd.mode_plausible(
            cheaper_mode, base.distance_km, base.international
        ):
            now = cd.transport_cost(mode, base.distance_km, req.travellers)
            then = cd.transport_cost(cheaper_mode, base.distance_km, req.travellers)
            saving = cd.from_inr(now - then, currency)
            if saving > 0:
                levers.append(
                    (
                        saving,
                        f"Travel by {cheaper_mode.value} instead of {mode.value}: saves "
                        f"about {_fmt(round_money(saving, currency), currency)} "
                        "(adds travel time).",
                    )
                )

        if req.pace is not Pace.RELAXED:
            now = cd.activity_cost_per_person_per_day(multiplier, req.pace, req.interests)
            then = cd.activity_cost_per_person_per_day(multiplier, Pace.RELAXED, req.interests)
            saving = cd.from_inr((now - then) * req.travellers * req.duration_days, currency)
            if saving > 0:
                levers.append(
                    (
                        saving,
                        "Drop to a relaxed pace with fewer paid activities: saves about "
                        f"{_fmt(round_money(saving, currency), currency)}.",
                    )
                )

        if req.duration_days > 2:
            per_day = (base.accommodation + base.food + base.activities + base.local_transport) / (
                req.duration_days
            )
            if per_day > 0:
                levers.append(
                    (
                        per_day,
                        f"Shorten the trip by one day: saves about "
                        f"{_fmt(round_money(per_day, currency), currency)}.",
                    )
                )

        levers.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in levers[:4]] or [
            "Raise the budget or pick a closer destination - the fixed travel cost "
            "dominates this trip."
        ]


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _ReferenceUnits:
    """Reference unit prices in the request's currency, used for clamping."""

    currency: Currency
    multiplier: float
    distance_km: float
    travellers: int
    stay_type: StayType
    pace: Pace
    interests: tuple
    activity_per_day: float
    local_transport_per_day: float
    misc_per_person: float

    @classmethod
    def build(cls, req: TripRequest, multiplier: float, base: TripPricing) -> _ReferenceUnits:
        def conv(inr: float) -> float:
            return cd.from_inr(inr, req.currency)

        return cls(
            currency=req.currency,
            multiplier=multiplier,
            distance_km=base.distance_km,
            travellers=req.travellers,
            stay_type=req.stay_type,
            pace=req.pace,
            interests=tuple(req.interests),
            activity_per_day=conv(
                cd.activity_cost_per_person_per_day(multiplier, req.pace, req.interests)
            ),
            local_transport_per_day=conv(cd.local_transport_per_person_per_day(multiplier)),
            misc_per_person=conv(cd.misc_per_person(multiplier, base.international)),
        )

    def meal_for(self, meal_type: MealType) -> float:
        return cd.from_inr(
            cd.meal_cost_per_person_per_day(self.multiplier, self.stay_type, (meal_type,)),
            self.currency,
        )

    def stay_night_for(self, stay_type: StayType, travellers: int) -> float:
        return cd.from_inr(
            cd.stay_cost_per_night(stay_type, travellers, self.multiplier), self.currency
        )

    def transport_leg_for(self, mode: TransportMode) -> float:
        """One-way reference fare for a single person (or vehicle) on this route."""
        rate = cd.TRANSPORT_RATES.get(mode, cd.TRANSPORT_RATES[TransportMode.FLIGHT])
        return cd.from_inr(rate.fare(self.distance_km), self.currency)


def _clamp(
    value: float,
    reference: float,
    kind: str,
    adjustments: list[str],
    label: str,
    currency: Currency,
) -> float:
    """Return ``value`` unless it is implausible against ``reference``.

    Records a human-readable note in ``adjustments`` whenever it intervenes, so
    the UI and the PDF can tell the user exactly what was corrected.
    """
    low_factor, high_factor = CLAMPS.get(kind, (0.0, math.inf))
    value = max(float(value), 0.0)
    if reference <= 0:
        return value

    low, high = reference * low_factor, reference * high_factor
    if value < low:
        adjustments.append(
            f"{label}: raised {_fmt(value, currency)} to "
            f"{_fmt(round_money(low, currency), currency)} - the quoted price was "
            "implausibly low for this destination."
        )
        return low
    if value > high:
        adjustments.append(
            f"{label}: reduced {_fmt(value, currency)} to "
            f"{_fmt(round_money(high, currency), currency)} - the quoted price was "
            "implausibly high for this destination."
        )
        return high
    return value


def _verdict_for(utilisation_pct: float) -> BudgetVerdict:
    if utilisation_pct < UNDER_BUDGET_BELOW_PCT:
        return BudgetVerdict.UNDER_BUDGET
    if utilisation_pct < ON_BUDGET_BELOW_PCT:
        return BudgetVerdict.ON_BUDGET
    if utilisation_pct <= NEAR_LIMIT_BELOW_PCT:
        return BudgetVerdict.NEAR_LIMIT
    return BudgetVerdict.OVER_BUDGET


def _fmt(amount: float, currency: Currency) -> str:
    from app.core.money import format_money

    return format_money(amount, currency)


def _label(stay_type: StayType) -> str:
    return stay_type.value.replace("_", " ")


_CATEGORY_NOTES: dict[CostCategory, str] = {
    CostCategory.TRANSPORT: "Return intercity travel for the whole party.",
    CostCategory.ACCOMMODATION: "Room cost for every night actually stayed.",
    CostCategory.FOOD: "All planned meals for every traveller.",
    CostCategory.ACTIVITIES: "Tickets, tours and paid experiences.",
    CostCategory.LOCAL_TRANSPORT: "Metro, autos, cabs and rentals at the destination.",
    CostCategory.MISC: "Visa, insurance, SIM, tips and buffer.",
}

#: One step down the accommodation ladder, used for savings advice.
_CHEAPER_STAY: dict[StayType, StayType] = {
    StayType.LUXURY_HOTEL: StayType.MID_HOTEL,
    StayType.RESORT: StayType.MID_HOTEL,
    StayType.MID_HOTEL: StayType.BUDGET_HOTEL,
    StayType.APARTMENT: StayType.GUESTHOUSE,
    StayType.BUDGET_HOTEL: StayType.GUESTHOUSE,
    StayType.HOMESTAY: StayType.GUESTHOUSE,
    StayType.GUESTHOUSE: StayType.HOSTEL,
}

#: One step down the transport ladder.
_CHEAPER_MODE: dict[TransportMode, TransportMode] = {
    TransportMode.FLIGHT: TransportMode.TRAIN,
    TransportMode.CAR: TransportMode.TRAIN,
    TransportMode.TRAIN: TransportMode.BUS,
}
