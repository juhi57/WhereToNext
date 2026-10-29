"""The final, costed itinerary returned to the user and rendered into the PDF.

Where :mod:`app.models.draft` holds unit prices supplied by the model, these
models hold *reconciled* amounts: every ``cost_total`` on this side of the
boundary was computed by :mod:`app.services.budget`, never by the model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import (
    ActivityCategory,
    BudgetVerdict,
    CostCategory,
    Currency,
    MealType,
    PlannerMode,
    StayType,
    TransportMode,
)
from app.models.trip import TripRequest


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Costed(BaseModel):
    """Mixin for anything carrying a per-person and a party-wide amount."""

    model_config = ConfigDict(str_strip_whitespace=True)

    cost_per_person: Annotated[float, Field(ge=0)] = 0.0
    cost_total: Annotated[float, Field(ge=0)] = 0.0


class TransportLeg(Costed):
    """A costed intercity hop."""

    mode: TransportMode
    from_place: str
    to_place: str
    depart_hint: str = ""
    duration_hint: str = ""
    description: str = ""
    booking_tip: str = ""


class Activity(Costed):
    """A costed timetabled slot."""

    start_time: str = ""
    title: str
    description: str = ""
    category: ActivityCategory = ActivityCategory.SIGHTSEEING
    duration_minutes: int = 0
    location: str = ""
    booking_tip: str = ""


class Meal(Costed):
    """A costed meal recommendation."""

    meal_type: MealType
    suggestion: str
    cuisine: str = ""
    location: str = ""
    dietary_note: str = ""


class StayPlan(Costed):
    """Accommodation for one night. ``cost_total`` is the whole party's bill."""

    name: str
    stay_type: StayType
    area: str = ""
    why: str = ""
    booking_tip: str = ""
    charged: bool = Field(
        default=True,
        description="False on the departure day, when no night is actually paid for.",
    )


class ItineraryDay(BaseModel):
    """One fully costed day."""

    model_config = ConfigDict(str_strip_whitespace=True)

    day_number: int
    day_date: date
    title: str
    theme: str = ""
    activities: list[Activity] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)
    stay: StayPlan | None = None
    local_transport_note: str = ""
    local_transport_total: Annotated[float, Field(ge=0)] = 0.0
    day_total: Annotated[float, Field(ge=0)] = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def busy_minutes(self) -> int:
        """Total scheduled activity time, useful for sanity-checking pace."""
        return sum(a.duration_minutes for a in self.activities)


class BudgetLine(BaseModel):
    """One of the six spend buckets."""

    category: CostCategory
    amount: Annotated[float, Field(ge=0)]
    share_pct: Annotated[float, Field(ge=0, le=100)] = 0.0
    note: str = ""


class BudgetBreakdown(BaseModel):
    """The reconciled cost of the plan measured against the user's budget."""

    currency: Currency
    lines: list[BudgetLine]
    total: Annotated[float, Field(ge=0)]
    per_person: Annotated[float, Field(ge=0)]
    per_person_per_day: Annotated[float, Field(ge=0)]
    budget_total: Annotated[float, Field(ge=0)]
    difference: float = Field(
        description="budget_total - total. Negative means the plan overshoots."
    )
    utilisation_pct: Annotated[float, Field(ge=0)]
    verdict: BudgetVerdict
    advice: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def within_budget(self) -> bool:
        return self.verdict is not BudgetVerdict.OVER_BUDGET

    def line_for(self, category: CostCategory) -> BudgetLine:
        """Look up one bucket, returning a zeroed line when absent."""
        for line in self.lines:
            if line.category is category:
                return line
        return BudgetLine(category=category, amount=0.0)


class PlannerMeta(BaseModel):
    """Provenance of the plan - shown in the UI so the user knows what made it."""

    mode: PlannerMode
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    cache_hit: bool = False
    adjustments: list[str] = Field(
        default_factory=list,
        description="What the cost engine changed or flagged after the model replied.",
    )


class Itinerary(BaseModel):
    """A complete, costed, exportable travel plan."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=_utcnow)

    request: TripRequest
    title: str
    destination_overview: str = ""
    best_time_note: str = ""
    food_scene_note: str = ""

    outbound: TransportLeg
    inbound: TransportLeg
    days: list[ItineraryDay]
    budget: BudgetBreakdown

    misc_note: str = ""
    misc_total: Annotated[float, Field(ge=0)] = 0.0
    packing_list: list[str] = Field(default_factory=list)
    travel_tips: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    meta: PlannerMeta

    @computed_field  # type: ignore[prop-decorator]
    @property
    def nights_booked(self) -> int:
        return sum(1 for d in self.days if d.stay is not None and d.stay.charged)

    def slug(self) -> str:
        """Filesystem-safe stem for the exported PDF."""
        raw = f"{self.request.destination}-{self.request.start_date}-{self.id}"
        keep = [c if c.isalnum() or c in "-_" else "-" for c in raw.lower()]
        return "".join(keep).strip("-").replace("--", "-")[:80]
