"""The user-facing trip brief.

:class:`TripRequest` is the single input to the planner. It is validated hard at
the edge so that neither the cost engine nor the model ever sees an impossible
trip (negative budget, return before departure, 400 travellers).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.enums import (
    Currency,
    FoodPreference,
    Interest,
    Pace,
    StayType,
    TransportMode,
)

#: Hard ceiling on trip length. Longer trips blow past a single model context
#: and are better modelled as several trips.
MAX_TRIP_DAYS = 30

#: How far in the past a start date may sit. One day of slack covers timezone
#: skew between the browser and the server.
MAX_BACKDATE = timedelta(days=1)


class TripRequest(BaseModel):
    """Everything the planner needs to know about the trip."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "origin": "Mumbai, India",
                "destination": "Bali, Indonesia",
                "start_date": "2026-12-10",
                "end_date": "2026-12-16",
                "adults": 2,
                "children": 0,
                "budget_total": 180000,
                "currency": "INR",
                "transport_modes": ["flight"],
                "stay_type": "mid_hotel",
                "food_preference": "vegetarian",
                "pace": "balanced",
                "interests": ["beaches", "food", "culture"],
            }
        },
    )

    origin: Annotated[str, Field(min_length=2, max_length=120)]
    destination: Annotated[str, Field(min_length=2, max_length=120)]

    start_date: date
    end_date: date

    adults: Annotated[int, Field(ge=1, le=20)] = 1
    children: Annotated[int, Field(ge=0, le=20)] = 0

    budget_total: Annotated[float, Field(gt=0, le=1_000_000_000)]
    currency: Currency = Currency.INR

    transport_modes: Annotated[list[TransportMode], Field(min_length=1, max_length=5)] = Field(
        default_factory=lambda: [TransportMode.FLIGHT]
    )
    stay_type: StayType = StayType.MID_HOTEL
    food_preference: FoodPreference = FoodPreference.NO_PREFERENCE
    pace: Pace = Pace.BALANCED
    interests: Annotated[list[Interest], Field(max_length=8)] = Field(default_factory=list)

    accessibility_needs: Annotated[str | None, Field(max_length=400)] = None
    notes: Annotated[str | None, Field(max_length=800)] = None

    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data: object) -> object:
        """Let a previously serialised request validate again.

        ``extra="forbid"`` is what we want at the API edge - a misspelled field
        should be an error, not a silently ignored one. But ``model_dump()``
        also emits the computed fields below, so feeding a stored itinerary back
        through validation would fail on keys this model itself produced.
        Stripping them here keeps both behaviours.
        """
        if isinstance(data, dict):
            computed = set(cls.model_computed_fields)
            if computed.intersection(data):
                return {k: v for k, v in data.items() if k not in computed}
        return data

    @field_validator("origin", "destination")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("transport_modes", "interests")
    @classmethod
    def _dedupe(cls, value: list[object]) -> list[object]:
        """Drop duplicates while preserving the order the user picked them in."""
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _check_dates(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.end_date - self.start_date > timedelta(days=MAX_TRIP_DAYS - 1):
            raise ValueError(f"trip must not exceed {MAX_TRIP_DAYS} days")
        if self.start_date < date.today() - MAX_BACKDATE:
            raise ValueError("start_date must not be in the past")
        return self

    @model_validator(mode="after")
    def _check_route(self) -> Self:
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("origin and destination must differ")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_days(self) -> int:
        """Calendar days inclusive of both the arrival and departure day."""
        return (self.end_date - self.start_date).days + 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def nights(self) -> int:
        """Nights of accommodation required (one fewer than ``duration_days``)."""
        return max(self.duration_days - 1, 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def travellers(self) -> int:
        return self.adults + self.children

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_per_person_per_day(self) -> float:
        return round(self.budget_total / (self.travellers * self.duration_days), 2)

    def primary_transport(self) -> TransportMode:
        """The mode used to price the intercity legs."""
        return self.transport_modes[0]
