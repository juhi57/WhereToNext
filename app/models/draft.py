"""The contract the language model fills in.

This is deliberately *not* the final :class:`~app.models.itinerary.Itinerary`.
The draft carries only what a model is actually good at - choosing places,
sequencing a day, writing a useful tip, and giving a *unit* price. It carries no
totals, no percentages and no verdicts, because:

1. Language models are unreliable at arithmetic, and a plan whose numbers do not
   add up destroys trust faster than a plan with fewer activities.
2. Keeping totals out of the schema means there is exactly one place in the
   codebase that can produce a total - :mod:`app.services.budget` - which is
   also the only place that needs testing for it.

Every field is required and no field is nullable, which keeps the generated JSON
Schema strict and gives the model no silent way to skip a section.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ActivityCategory, MealType, StayType, TransportMode


class DraftBase(BaseModel):
    """Strict base: unknown keys are a hard error, not a silent discard."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DraftTransportLeg(DraftBase):
    """One intercity hop, priced per person."""

    mode: TransportMode
    from_place: Annotated[str, Field(max_length=120)]
    to_place: Annotated[str, Field(max_length=120)]
    depart_hint: Annotated[str, Field(max_length=80, description="e.g. 'early morning', '08:40'")]
    duration_hint: Annotated[str, Field(max_length=80, description="e.g. '6h 20m incl. layover'")]
    description: Annotated[str, Field(max_length=600)]
    estimated_cost_per_person: Annotated[float, Field(ge=0)]
    booking_tip: Annotated[str, Field(max_length=400)]


class DraftActivity(DraftBase):
    """A single timetabled slot in a day, priced per person."""

    start_time: Annotated[str, Field(max_length=16, description="24h clock, e.g. '09:30'")]
    title: Annotated[str, Field(max_length=160)]
    description: Annotated[str, Field(max_length=800)]
    category: ActivityCategory
    duration_minutes: Annotated[int, Field(ge=0, le=1440)]
    location: Annotated[str, Field(max_length=160)]
    estimated_cost_per_person: Annotated[float, Field(ge=0)]
    booking_tip: Annotated[str, Field(max_length=400)]


class DraftMeal(DraftBase):
    """A meal recommendation that already respects the dietary constraint."""

    meal_type: MealType
    suggestion: Annotated[str, Field(max_length=200)]
    cuisine: Annotated[str, Field(max_length=120)]
    location: Annotated[str, Field(max_length=160)]
    dietary_note: Annotated[
        str, Field(max_length=300, description="How this fits the stated food preference")
    ]
    estimated_cost_per_person: Annotated[float, Field(ge=0)]


class DraftStay(DraftBase):
    """Where the traveller sleeps that night, priced for the whole party."""

    name: Annotated[str, Field(max_length=160)]
    stay_type: StayType
    area: Annotated[str, Field(max_length=160)]
    why: Annotated[str, Field(max_length=500, description="Why this area suits this trip")]
    estimated_cost_per_night_total: Annotated[
        float, Field(ge=0, description="For the entire party, not per person")
    ]
    booking_tip: Annotated[str, Field(max_length=400)]


class DraftDay(DraftBase):
    """One day of the trip."""

    day_number: Annotated[int, Field(ge=1, le=30)]
    title: Annotated[str, Field(max_length=160)]
    theme: Annotated[str, Field(max_length=200)]
    activities: Annotated[list[DraftActivity], Field(min_length=1, max_length=8)]
    meals: Annotated[list[DraftMeal], Field(max_length=4)]
    stay: DraftStay
    local_transport_note: Annotated[str, Field(max_length=300)]
    local_transport_cost_per_person: Annotated[float, Field(ge=0)]


class ItineraryDraft(DraftBase):
    """The complete model response for one trip."""

    title: Annotated[str, Field(max_length=180)]
    destination_overview: Annotated[str, Field(max_length=1200)]
    best_time_note: Annotated[
        str, Field(max_length=500, description="Weather and seasonality for these exact dates")
    ]
    food_scene_note: Annotated[str, Field(max_length=600)]
    outbound: DraftTransportLeg
    inbound: DraftTransportLeg
    days: Annotated[list[DraftDay], Field(min_length=1, max_length=30)]
    misc_cost_per_person: Annotated[
        float, Field(ge=0, description="SIM, visa, tips, buffer - per person for the whole trip")
    ]
    misc_note: Annotated[str, Field(max_length=400)]
    packing_list: Annotated[list[str], Field(max_length=25)]
    travel_tips: Annotated[list[str], Field(max_length=15)]
    warnings: Annotated[
        list[str], Field(max_length=10, description="Scams, closures, permits, safety")
    ]
