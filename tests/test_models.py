"""Request validation, derived values and money formatting."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.core.money import format_money, round_money
from app.models.enums import Currency, Interest, TransportMode
from app.models.trip import MAX_TRIP_DAYS, TripRequest


def _base(start: date, **overrides: object) -> dict[str, object]:
    return {
        "origin": "Mumbai",
        "destination": "Goa",
        "start_date": start,
        "end_date": start + timedelta(days=3),
        "budget_total": 50_000,
        **overrides,
    }


class TestTripRequest:
    def test_derived_values(self, start_date: date) -> None:
        req = TripRequest(**_base(start_date, adults=2, children=1))
        assert req.duration_days == 4
        assert req.nights == 3
        assert req.travellers == 3
        assert req.budget_per_person_per_day == pytest.approx(50_000 / (3 * 4))

    def test_single_day_trip_has_no_nights(self, start_date: date) -> None:
        req = TripRequest(**_base(start_date, end_date=start_date))
        assert req.duration_days == 1
        assert req.nights == 0

    def test_return_before_departure_is_rejected(self, start_date: date) -> None:
        with pytest.raises(ValidationError, match="on or after"):
            TripRequest(**_base(start_date, end_date=start_date - timedelta(days=1)))

    def test_trip_length_is_capped(self, start_date: date) -> None:
        too_long = start_date + timedelta(days=MAX_TRIP_DAYS)
        with pytest.raises(ValidationError, match="must not exceed"):
            TripRequest(**_base(start_date, end_date=too_long))

    def test_past_departure_is_rejected(self) -> None:
        past = date.today() - timedelta(days=10)
        with pytest.raises(ValidationError, match="must not be in the past"):
            TripRequest(**_base(past))

    def test_same_origin_and_destination_is_rejected(self, start_date: date) -> None:
        with pytest.raises(ValidationError, match="must differ"):
            TripRequest(**_base(start_date, destination="mumbai"))

    @pytest.mark.parametrize("budget", [0, -1])
    def test_budget_must_be_positive(self, start_date: date, budget: float) -> None:
        with pytest.raises(ValidationError):
            TripRequest(**_base(start_date, budget_total=budget))

    def test_unknown_field_is_rejected(self, start_date: date) -> None:
        """Typos in the payload are an error, not a silent no-op."""
        with pytest.raises(ValidationError, match=r"extra_forbidden|not permitted"):
            TripRequest(**_base(start_date, destinaton="Goa"))

    def test_duplicate_interests_are_collapsed_in_order(self, start_date: date) -> None:
        req = TripRequest(
            **_base(
                start_date,
                interests=[Interest.FOOD, Interest.BEACHES, Interest.FOOD],
            )
        )
        assert req.interests == [Interest.FOOD, Interest.BEACHES]

    def test_whitespace_is_stripped(self, start_date: date) -> None:
        req = TripRequest(**_base(start_date, origin="  Mumbai  "))
        assert req.origin == "Mumbai"

    def test_round_trips_through_serialisation(self, trip: TripRequest) -> None:
        """A stored request must validate again despite its computed fields."""
        restored = TripRequest.model_validate(trip.model_dump(mode="json"))
        assert restored == trip

    def test_primary_transport_is_the_first_choice(self, start_date: date) -> None:
        req = TripRequest(
            **_base(start_date, transport_modes=[TransportMode.TRAIN, TransportMode.BUS])
        )
        assert req.primary_transport() is TransportMode.TRAIN


class TestMoney:
    @pytest.mark.parametrize(
        ("amount", "currency", "expected"),
        [
            (125_000, Currency.INR, "₹1,25,000"),
            (1_234_567, Currency.INR, "₹12,34,567"),
            (999, Currency.INR, "₹999"),
            (1_234.5, Currency.USD, "$1,234.50"),
            (980, Currency.JPY, "¥980"),
        ],
    )
    def test_formatting(self, amount: float, currency: Currency, expected: str) -> None:
        assert format_money(amount, currency) == expected

    def test_indian_grouping_differs_from_western(self) -> None:
        """Lakhs and crores group as 12,34,567 - not 1,234,567."""
        assert format_money(1_234_567, Currency.INR) == "₹12,34,567"
        assert format_money(1_234_567, Currency.USD) == "$1,234,567.00"

    def test_ascii_safe_avoids_the_rupee_glyph(self) -> None:
        """ReportLab's base fonts have no U+20B9, so the PDF needs a fallback."""
        plain = format_money(125_000, Currency.INR, ascii_safe=True)
        assert plain == "Rs. 1,25,000"
        assert "₹" not in plain

    def test_zero_decimal_currencies_round_to_whole_units(self) -> None:
        assert round_money(99.6, Currency.INR) == 100.0
        assert round_money(99.996, Currency.USD) == 100.0

    def test_negative_amounts_clamp_to_zero(self) -> None:
        """A cost line can be free; it can never be negative."""
        assert round_money(-5, Currency.INR) == 0.0
