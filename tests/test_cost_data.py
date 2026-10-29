"""Place lookup, geometry and the reference pricing primitives."""

from __future__ import annotations

import json
import math
from importlib import resources

import pytest

from app.models.enums import Currency, Pace, StayType, TransportMode
from app.services import cost_data as cd


class TestDataset:
    def test_dataset_is_internally_consistent(self) -> None:
        """Catch a bad hand-edit of places.json before it reaches pricing."""
        raw = json.loads(
            resources.files("app.data").joinpath("places.json").read_text(encoding="utf-8")
        )
        tiers = raw["tier_multipliers"]
        countries = {c.casefold() for c in raw["country_tiers"]}
        names, aliases = set(), set()

        for entry in raw["places"]:
            assert -90 <= entry["lat"] <= 90, entry["name"]
            assert -180 <= entry["lon"] <= 180, entry["name"]
            assert entry["tier"] in tiers, entry["name"]
            assert entry["country"].casefold() in countries, entry["country"]
            assert entry["name"] not in names, f"duplicate place {entry['name']}"
            names.add(entry["name"])
            for alias in entry["aliases"]:
                assert alias not in aliases, f"duplicate alias {alias}"
                aliases.add(alias)

    def test_every_tier_has_a_multiplier(self) -> None:
        book = cd.place_book()
        for place in book.places:
            assert place.multiplier > 0


class TestResolution:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Mumbai", "Mumbai"),
            ("bombay", "Mumbai"),
            ("BOM", "Mumbai"),
            ("North Goa, India", "Goa"),
            ("New Delhi", "Delhi"),
            ("ubud", "Bali"),
            ("Bali, Indonesia", "Bali"),
            ("  kuala lumpur  ", "Kuala Lumpur"),
        ],
    )
    def test_resolves_names_and_aliases(self, query: str, expected: str) -> None:
        place = cd.place_book().resolve(query)
        assert place is not None and place.name == expected

    def test_unknown_place_resolves_to_none(self) -> None:
        assert cd.place_book().resolve("Narnia") is None

    def test_country_tier_is_the_fallback_for_unknown_cities(self) -> None:
        """An unlisted town in Switzerland must still price as expensive."""
        tier, multiplier = cd.place_book().tier_for_text("a tiny village in Switzerland")
        assert tier == "very_expensive"
        assert multiplier > 2

    def test_completely_unknown_text_defaults_to_moderate(self) -> None:
        """Never price an unrecognised destination as if it were free."""
        tier, multiplier = cd.place_book().tier_for_text("Narnia")
        assert tier == "moderate"
        assert multiplier == pytest.approx(1.0)

    def test_longer_names_win_over_substrings(self) -> None:
        place = cd.place_book().resolve("Ho Chi Minh City")
        assert place is not None and place.name == "Ho Chi Minh City"


class TestGeometry:
    def test_haversine_matches_a_known_distance(self) -> None:
        """Mumbai to Delhi is about 1,150 km great-circle."""
        km = cd.haversine_km(19.0760, 72.8777, 28.6139, 77.2090)
        assert 1100 < km < 1200

    def test_haversine_is_zero_for_identical_points(self) -> None:
        assert cd.haversine_km(10, 20, 10, 20) == pytest.approx(0.0, abs=1e-9)

    def test_haversine_is_symmetric(self) -> None:
        there = cd.haversine_km(19.07, 72.87, -8.67, 115.21)
        back = cd.haversine_km(-8.67, 115.21, 19.07, 72.87)
        assert there == pytest.approx(back)

    def test_known_route_is_not_flagged_as_estimated(self) -> None:
        km, estimated = cd.distance_between("Mumbai", "Bali")
        assert estimated is False
        assert 5000 < km < 6000

    def test_unknown_route_falls_back_and_says_so(self) -> None:
        km, estimated = cd.distance_between("Narnia", "Atlantis")
        assert estimated is True
        assert km > 0

    def test_international_detection(self) -> None:
        assert cd.is_international("Mumbai", "Bali") is True
        assert cd.is_international("Mumbai", "Goa") is False

    def test_unknown_endpoints_are_treated_as_domestic(self) -> None:
        """Better to omit a visa line than to invent one."""
        assert cd.is_international("Narnia", "Mumbai") is False


class TestModePlausibility:
    @pytest.mark.parametrize(
        ("mode", "km", "international", "expected"),
        [
            (TransportMode.FLIGHT, 5571, True, True),
            (TransportMode.TRAIN, 5571, True, False),
            (TransportMode.TRAIN, 411, False, True),
            (TransportMode.BUS, 2000, False, False),
            (TransportMode.CAR, 411, False, True),
            (TransportMode.CAR, 411, True, False),
        ],
    )
    def test_rules_out_the_impossible(
        self, mode: TransportMode, km: float, international: bool, expected: bool
    ) -> None:
        assert cd.mode_plausible(mode, km, international) is expected


class TestPricing:
    def test_fares_scale_with_distance(self) -> None:
        near = cd.transport_cost(TransportMode.FLIGHT, 400, 1)
        far = cd.transport_cost(TransportMode.FLIGHT, 5000, 1)
        assert far > near * 3

    def test_fare_has_a_floor(self) -> None:
        """A one-kilometre flight still costs what a flight costs."""
        rate = cd.TRANSPORT_RATES[TransportMode.FLIGHT]
        assert cd.transport_cost(TransportMode.FLIGHT, 1, 1, round_trip=False) >= rate.minimum

    def test_per_person_modes_scale_with_party_size(self) -> None:
        one = cd.transport_cost(TransportMode.FLIGHT, 1000, 1)
        three = cd.transport_cost(TransportMode.FLIGHT, 1000, 3)
        assert three == pytest.approx(one * 3)

    def test_a_hired_car_is_priced_per_vehicle(self) -> None:
        one = cd.transport_cost(TransportMode.CAR, 300, 1)
        four = cd.transport_cost(TransportMode.CAR, 300, 4)
        assert one == pytest.approx(four)

    def test_round_trip_is_double_one_way(self) -> None:
        single = cd.transport_cost(TransportMode.TRAIN, 800, 2, round_trip=False)
        both = cd.transport_cost(TransportMode.TRAIN, 800, 2, round_trip=True)
        assert both == pytest.approx(single * 2)

    def test_hostels_are_priced_per_bed_not_per_room(self) -> None:
        assert cd.rooms_needed(3, StayType.HOSTEL) == 3
        assert cd.rooms_needed(3, StayType.MID_HOTEL) == 2

    def test_rooms_round_up_for_odd_party_sizes(self) -> None:
        assert cd.rooms_needed(5, StayType.MID_HOTEL) == 3
        assert cd.rooms_needed(1, StayType.MID_HOTEL) == 1

    def test_expensive_tiers_cost_more(self) -> None:
        cheap = cd.stay_cost_per_night(StayType.MID_HOTEL, 2, 0.65)
        dear = cd.stay_cost_per_night(StayType.MID_HOTEL, 2, 2.8)
        assert dear > cheap * 3

    def test_stay_tier_drags_the_food_budget(self) -> None:
        hostel = cd.meal_cost_per_person_per_day(1.0, StayType.HOSTEL)
        luxury = cd.meal_cost_per_person_per_day(1.0, StayType.LUXURY_HOTEL)
        assert luxury > hostel * 2

    def test_pace_and_interests_raise_activity_cost(self) -> None:
        from app.models.enums import Interest

        calm = cd.activity_cost_per_person_per_day(1.0, Pace.RELAXED, [])
        busy = cd.activity_cost_per_person_per_day(1.0, Pace.PACKED, [])
        thrill = cd.activity_cost_per_person_per_day(1.0, Pace.PACKED, [Interest.ADVENTURE])
        assert calm < busy < thrill

    def test_international_trips_carry_extra_fixed_costs(self) -> None:
        assert cd.misc_per_person(1.0, True) > cd.misc_per_person(1.0, False)


class TestCurrency:
    def test_conversion_round_trips(self) -> None:
        inr = cd.to_inr(1000, Currency.USD)
        assert cd.from_inr(inr, Currency.USD) == pytest.approx(1000)

    def test_inr_is_the_identity(self) -> None:
        assert cd.to_inr(500, Currency.INR) == 500
        assert cd.from_inr(500, Currency.INR) == 500

    def test_malformed_override_does_not_break_the_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WTN_FX_RATES", "{not json")
        cd.fx_rates.cache_clear()
        try:
            assert cd.fx_rates()[Currency.USD] > 0
        finally:
            cd.fx_rates.cache_clear()

    def test_override_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WTN_FX_RATES", '{"USD": 100.0}')
        cd.fx_rates.cache_clear()
        try:
            assert cd.fx_rates()[Currency.USD] == pytest.approx(100.0)
        finally:
            cd.fx_rates.cache_clear()


def test_mode_rate_fare_is_monotonic() -> None:
    rate = cd.TRANSPORT_RATES[TransportMode.TRAIN]
    distances = [0, 100, 500, 1000, 2000]
    fares = [rate.fare(d) for d in distances]
    assert fares == sorted(fares)
    assert all(math.isfinite(f) for f in fares)
