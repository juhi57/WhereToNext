"""Reference pricing data and place lookup for the offline cost model.

Everything here is **indicative**: the point is to produce a plausible, internally
consistent budget without depending on a paid flight/hotel API, so the project
runs for anyone who clones it. All base rates are expressed in INR at tier
multiplier ``1.0`` (a mid-range Indian metro) and are scaled by:

* the destination's **price tier** (``very_cheap`` .. ``very_expensive``),
* the chosen **stay type** (a resort traveller also eats pricier meals),
* the chosen **pace** and **interests** (adventure days cost more than museum days).

Distances come from the great-circle distance between city centroids in
``app/data/places.json``, inflated by a per-mode detour factor, because roads and
flight paths are never straight lines.

To wire in live pricing, replace :func:`transport_cost` and
:func:`stay_cost_per_night` - nothing else in the codebase reads these tables.
"""

from __future__ import annotations

import json
import math
import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Final

from app.models.enums import (
    Currency,
    Interest,
    MealType,
    Pace,
    StayType,
    TransportMode,
)

# --------------------------------------------------------------------------- #
# Base rates (INR, tier multiplier 1.0)
# --------------------------------------------------------------------------- #

#: Per room per night, except where :data:`STAY_PRICED_PER_PERSON` says otherwise.
STAY_PER_NIGHT: Final[dict[StayType, float]] = {
    StayType.HOSTEL: 900.0,
    StayType.GUESTHOUSE: 1600.0,
    StayType.BUDGET_HOTEL: 2600.0,
    StayType.HOMESTAY: 2300.0,
    StayType.APARTMENT: 4000.0,
    StayType.MID_HOTEL: 5000.0,
    StayType.RESORT: 9500.0,
    StayType.LUXURY_HOTEL: 15000.0,
}

#: Hostels sell dorm beds, so the rate multiplies by head count, not room count.
STAY_PRICED_PER_PERSON: Final[frozenset[StayType]] = frozenset({StayType.HOSTEL})

#: People sharing one room for every other stay type.
ROOM_OCCUPANCY: Final[int] = 2

#: How much a given accommodation tier drags the food budget with it.
DINING_UPLIFT: Final[dict[StayType, float]] = {
    StayType.HOSTEL: 0.75,
    StayType.GUESTHOUSE: 0.85,
    StayType.BUDGET_HOTEL: 0.90,
    StayType.HOMESTAY: 0.90,
    StayType.APARTMENT: 0.95,
    StayType.MID_HOTEL: 1.00,
    StayType.RESORT: 1.40,
    StayType.LUXURY_HOTEL: 1.80,
}

#: Per person per meal.
MEAL_BASE: Final[dict[MealType, float]] = {
    MealType.BREAKFAST: 160.0,
    MealType.LUNCH: 360.0,
    MealType.DINNER: 460.0,
    MealType.SNACK: 130.0,
}

#: Per person per day of paid attractions, tickets and experiences.
ACTIVITY_PER_DAY: Final[dict[Pace, float]] = {
    Pace.RELAXED: 650.0,
    Pace.BALANCED: 1150.0,
    Pace.PACKED: 1700.0,
}

#: Interests that make a day materially more expensive than sightseeing.
ACTIVITY_UPLIFT: Final[dict[Interest, float]] = {
    Interest.ADVENTURE: 1.35,
    Interest.WILDLIFE: 1.40,
    Interest.WELLNESS: 1.30,
    Interest.NIGHTLIFE: 1.20,
    Interest.SHOPPING: 1.15,
}

#: Per person per day of metro, autos, cabs and scooter hire at the destination.
LOCAL_TRANSPORT_PER_DAY: Final[float] = 320.0

#: Per person, whole trip: SIM, tips, laundry, small buffer.
MISC_PER_PERSON: Final[float] = 1600.0

#: Added per person when the trip crosses a border: visa, insurance, forex markup.
INTERNATIONAL_MISC_PER_PERSON: Final[float] = 4500.0


@dataclass(frozen=True, slots=True)
class ModeRate:
    """Fare model for one intercity transport mode.

    The fare is ``base + per_km * distance``, floored at ``minimum``. ``detour``
    inflates great-circle distance into realistic routed distance.
    """

    base: float
    per_km: float
    detour: float
    minimum: float
    per_person: bool

    def fare(self, straight_km: float) -> float:
        """Fare for one leg over ``straight_km`` of great-circle distance."""
        routed = max(straight_km, 0.0) * self.detour
        return max(self.base + self.per_km * routed, self.minimum)


TRANSPORT_RATES: Final[dict[TransportMode, ModeRate]] = {
    TransportMode.FLIGHT: ModeRate(2000.0, 2.20, 1.05, 2400.0, per_person=True),
    TransportMode.TRAIN: ModeRate(150.0, 1.30, 1.25, 250.0, per_person=True),
    TransportMode.BUS: ModeRate(100.0, 1.60, 1.30, 200.0, per_person=True),
    # A car is hired per vehicle, so this rate covers the whole party.
    TransportMode.CAR: ModeRate(600.0, 13.00, 1.30, 900.0, per_person=False),
    TransportMode.FERRY: ModeRate(400.0, 3.00, 1.15, 600.0, per_person=True),
}

#: Longest distance (km) and whether border crossing is realistic, per mode.
#: Used to reject nonsense like "take the train from Mumbai to Bali" before it
#: reaches the user as advice.
MODE_LIMITS: Final[dict[TransportMode, tuple[float, bool]]] = {
    TransportMode.FLIGHT: (20000.0, True),
    TransportMode.TRAIN: (3000.0, False),
    TransportMode.BUS: (1500.0, False),
    TransportMode.CAR: (1200.0, False),
    TransportMode.FERRY: (2000.0, True),
}


def mode_plausible(mode: TransportMode, distance_km: float, international: bool) -> bool:
    """Whether ``mode`` can realistically cover this route.

    Deliberately conservative: it only has centroid distance and a country
    comparison to go on, so it rules out the clearly impossible (a bus to
    Tokyo) rather than trying to model every rail network.
    """
    max_km, crosses_borders = MODE_LIMITS.get(mode, (math.inf, True))
    if distance_km > max_km:
        return False
    return crosses_borders or not international


#: Indicative INR per one unit of the given currency. Override with the
#: ``WTN_FX_RATES`` env var (JSON object) or swap in a live FX feed.
DEFAULT_FX_INR: Final[dict[Currency, float]] = {
    Currency.INR: 1.0,
    Currency.USD: 88.0,
    Currency.EUR: 95.0,
    Currency.GBP: 112.0,
    Currency.AED: 24.0,
    Currency.SGD: 65.0,
    Currency.JPY: 0.58,
    Currency.AUD: 57.0,
}

#: Distance assumed when neither endpoint can be resolved to a known place.
FALLBACK_DOMESTIC_KM: Final[float] = 900.0
FALLBACK_UNKNOWN_KM: Final[float] = 3200.0

EARTH_RADIUS_KM: Final[float] = 6371.0088


# --------------------------------------------------------------------------- #
# Place lookup
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Place:
    """A resolved destination with coordinates and a price tier."""

    name: str
    country: str
    lat: float
    lon: float
    tier: str
    multiplier: float


@dataclass(frozen=True, slots=True)
class PlaceBook:
    """The loaded reference dataset, indexed for lookup."""

    tier_multipliers: dict[str, float]
    country_tiers: dict[str, str]
    places: tuple[Place, ...]
    _by_key: dict[str, Place]

    def resolve(self, query: str) -> Place | None:
        """Best-effort match of free text like ``"North Goa, India"`` to a place.

        Tried in order: exact key, alias/name contained in the query, then token
        overlap. Returns ``None`` when nothing matches, which callers treat as
        "fall back to the country tier".
        """
        key = _normalise(query)
        if not key:
            return None
        if key in self._by_key:
            return self._by_key[key]

        # Longest keys first so "new delhi" wins over "delhi" and
        # "ho chi minh city" over a stray "chi".
        for candidate in sorted(self._by_key, key=len, reverse=True):
            if len(candidate) < 4:
                continue
            if candidate in key:
                return self._by_key[candidate]

        tokens = set(key.split())
        best: tuple[int, Place] | None = None
        for candidate, place in self._by_key.items():
            overlap = len(tokens & set(candidate.split()))
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, place)
        return best[1] if best else None

    def tier_for_text(self, query: str) -> tuple[str, float]:
        """Price tier for free text, using the country table when the city is unknown.

        Falls back to ``moderate`` so an unrecognised destination is never priced
        as if it were free.
        """
        place = self.resolve(query)
        if place is not None:
            return place.tier, place.multiplier

        key = _normalise(query)
        for country, tier in self.country_tiers.items():
            if country in key:
                return tier, self.tier_multipliers.get(tier, 1.0)
        return "moderate", self.tier_multipliers.get("moderate", 1.0)

    def country_of(self, query: str) -> str | None:
        """Country for free text, or ``None`` when undeterminable."""
        place = self.resolve(query)
        if place is not None:
            return place.country
        key = _normalise(query)
        for country in self.country_tiers:
            if country in key:
                return country.title()
        return None


def _normalise(text: str) -> str:
    """Casefold, strip accents and collapse punctuation to single spaces."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() else " " for c in ascii_only.casefold())
    return " ".join(cleaned.split())


@lru_cache(maxsize=1)
def place_book() -> PlaceBook:
    """Load and index ``app/data/places.json`` once per process."""
    raw = resources.files("app.data").joinpath("places.json").read_text(encoding="utf-8")
    data = json.loads(raw)

    multipliers: dict[str, float] = {k: float(v) for k, v in data["tier_multipliers"].items()}
    countries: dict[str, str] = dict(data["country_tiers"])

    places: list[Place] = []
    index: dict[str, Place] = {}
    for entry in data["places"]:
        tier = entry["tier"]
        place = Place(
            name=entry["name"],
            country=entry["country"],
            lat=float(entry["lat"]),
            lon=float(entry["lon"]),
            tier=tier,
            multiplier=multipliers.get(tier, 1.0),
        )
        places.append(place)
        for key in (place.name, *entry.get("aliases", ())):
            index.setdefault(_normalise(key), place)

    return PlaceBook(
        tier_multipliers=multipliers,
        country_tiers=countries,
        places=tuple(places),
        _by_key=index,
    )


# --------------------------------------------------------------------------- #
# Geometry and currency
# --------------------------------------------------------------------------- #


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two coordinates."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def distance_between(origin: str, destination: str) -> tuple[float, bool]:
    """Distance in km between two free-text places, and whether it was estimated.

    The second element is ``True`` when at least one endpoint could not be
    resolved and a fallback distance was substituted - the planner surfaces that
    as a caveat rather than pretending to precision it does not have.
    """
    book = place_book()
    a, b = book.resolve(origin), book.resolve(destination)
    if a is not None and b is not None:
        return haversine_km(a.lat, a.lon, b.lat, b.lon), False

    same_country = (book.country_of(origin) or "?") == (book.country_of(destination) or "??")
    return (FALLBACK_DOMESTIC_KM if same_country else FALLBACK_UNKNOWN_KM), True


def is_international(origin: str, destination: str) -> bool:
    """Whether the trip crosses a border, as far as the dataset can tell.

    Unknown endpoints are treated as domestic so the planner does not invent a
    visa line for a trip that may not need one.
    """
    book = place_book()
    a, b = book.country_of(origin), book.country_of(destination)
    if a is None or b is None:
        return False
    return a.casefold() != b.casefold()


@lru_cache(maxsize=1)
def fx_rates() -> dict[Currency, float]:
    """INR per unit of each supported currency, with env-var overrides."""
    rates = dict(DEFAULT_FX_INR)
    override = os.getenv("WTN_FX_RATES")
    if override:
        try:
            for code, value in json.loads(override).items():
                rates[Currency(code.upper())] = float(value)
        except (ValueError, TypeError, KeyError):
            # A malformed override must not take the app down; the indicative
            # defaults are always a usable fallback.
            pass
    return rates


def from_inr(amount_inr: float, currency: Currency) -> float:
    """Convert an INR amount into ``currency``."""
    rate = fx_rates().get(currency, 1.0)
    return amount_inr / rate if rate else amount_inr


def to_inr(amount: float, currency: Currency) -> float:
    """Convert an amount in ``currency`` into INR."""
    return amount * fx_rates().get(currency, 1.0)


# --------------------------------------------------------------------------- #
# Priced components (all returns are INR)
# --------------------------------------------------------------------------- #


def rooms_needed(travellers: int, stay_type: StayType) -> int:
    """Billable units of accommodation: dorm beds for hostels, rooms otherwise."""
    if stay_type in STAY_PRICED_PER_PERSON:
        return max(travellers, 1)
    return max(math.ceil(travellers / ROOM_OCCUPANCY), 1)


def stay_cost_per_night(stay_type: StayType, travellers: int, multiplier: float) -> float:
    """Whole-party accommodation cost for one night, in INR."""
    rate = STAY_PER_NIGHT.get(stay_type, STAY_PER_NIGHT[StayType.MID_HOTEL])
    return rate * multiplier * rooms_needed(travellers, stay_type)


def transport_cost(
    mode: TransportMode, distance_km: float, travellers: int, round_trip: bool = True
) -> float:
    """Intercity transport cost in INR for the whole party.

    ``round_trip`` prices the return leg at the same rate, which is what a
    traveller actually books for a there-and-back holiday.
    """
    rate = TRANSPORT_RATES.get(mode, TRANSPORT_RATES[TransportMode.FLIGHT])
    one_way = rate.fare(distance_km)
    if rate.per_person:
        one_way *= max(travellers, 1)
    return one_way * (2 if round_trip else 1)


def meal_cost_per_person_per_day(
    multiplier: float, stay_type: StayType, meals: tuple[MealType, ...] | None = None
) -> float:
    """Per-person daily food cost in INR for the given meals (default: all three)."""
    chosen = meals or (MealType.BREAKFAST, MealType.LUNCH, MealType.DINNER)
    uplift = DINING_UPLIFT.get(stay_type, 1.0)
    return sum(MEAL_BASE.get(m, 0.0) for m in chosen) * multiplier * uplift


def activity_cost_per_person_per_day(
    multiplier: float, pace: Pace, interests: list[Interest] | tuple[Interest, ...]
) -> float:
    """Per-person daily activity cost in INR, biased by pace and interests."""
    base = ACTIVITY_PER_DAY.get(pace, ACTIVITY_PER_DAY[Pace.BALANCED])
    uplift = max((ACTIVITY_UPLIFT.get(i, 1.0) for i in interests), default=1.0)
    return base * multiplier * uplift


def local_transport_per_person_per_day(multiplier: float) -> float:
    """Per-person daily local transport cost in INR."""
    return LOCAL_TRANSPORT_PER_DAY * multiplier


def misc_per_person(multiplier: float, international: bool) -> float:
    """Per-person whole-trip miscellaneous cost in INR."""
    base = MISC_PER_PERSON * multiplier
    return base + (INTERNATIONAL_MISC_PER_PERSON if international else 0.0)
