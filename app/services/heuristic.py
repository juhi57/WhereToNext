"""Template planner used when no API key is configured, or when the model fails.

This exists for three reasons, in order of importance:

1. **The repository runs for anyone who clones it.** A reviewer with no
   Anthropic key still gets a complete, costed, exportable itinerary.
2. **Tests stay fast, free and deterministic.** Seeding from the request means
   the same brief always yields the same plan, so the cost engine and the PDF
   renderer can be tested against a fixed fixture.
3. **It is a real fallback.** If the API is down, rate-limited or returns
   something unparseable, the user gets a usable plan with an honest label
   rather than a 500.

It emits an :class:`~app.models.draft.ItineraryDraft` - exactly what the model
emits - so both engines feed the identical reconciliation path in
:mod:`app.services.budget`. There is only one way to cost a plan in this codebase.
"""

from __future__ import annotations

import hashlib
import random
from enum import IntEnum
from typing import Final

from app.models.draft import (
    DraftActivity,
    DraftDay,
    DraftMeal,
    DraftStay,
    DraftTransportLeg,
    ItineraryDraft,
)
from app.models.enums import (
    ActivityCategory,
    FoodPreference,
    Interest,
    MealType,
    Pace,
    StayType,
    TransportMode,
)
from app.models.trip import TripRequest
from app.services import cost_data as cd
from app.services.budget import BudgetEngine, BudgetEnvelope

#: Paid activities scheduled per full day, by pace.
ACTIVITIES_PER_DAY: Final[dict[Pace, int]] = {
    Pace.RELAXED: 2,
    Pace.BALANCED: 3,
    Pace.PACKED: 4,
}


class Slot(IntEnum):
    """When in the day an activity belongs.

    Activities are sorted by this before clock times are assigned, so a sunset
    viewpoint can never be scheduled for nine in the morning.
    """

    MORNING = 0
    MIDDAY = 1
    AFTERNOON = 2
    EVENING = 3


#: Earliest sensible start for each time of day, in minutes past midnight.
SLOT_EARLIEST: Final[dict[Slot, int]] = {
    Slot.MORNING: 9 * 60,
    Slot.MIDDAY: 11 * 60 + 30,
    Slot.AFTERNOON: 14 * 60 + 30,
    Slot.EVENING: 18 * 60 + 30,
}

#: When the day's schedule starts, by day shape.
DAY_START: Final[int] = 9 * 60
ARRIVAL_DAY_START: Final[int] = 14 * 60
DEPARTURE_DAY_START: Final[int] = 8 * 60 + 30

#: Buffer left between the end of one activity and the start of the next.
TRANSITION_MINUTES: Final[int] = 30

#: Nothing is scheduled to begin after this, however full the day is.
LATEST_START: Final[int] = 21 * 60

_Archetype = tuple[str, str, ActivityCategory, int, Slot]

#: Activity archetypes per interest. ``{dest}`` is substituted with the
#: destination so the text reads naturally without inventing venue names the
#: planner cannot verify. The final element is the time of day the activity
#: actually makes sense at.
ARCHETYPES: Final[dict[Interest, tuple[_Archetype, ...]]] = {
    Interest.NATURE: (
        (
            "Morning walk through {dest}'s green belt",
            "An easy start among the parks and waterfronts on the edge of {dest}.",
            ActivityCategory.SIGHTSEEING,
            120,
            Slot.MORNING,
        ),
        (
            "Viewpoint hike above {dest}",
            "A moderate climb to the best panorama over {dest}. Carry water and start before the heat.",  # noqa: E501
            ActivityCategory.ADVENTURE,
            180,
            Slot.MORNING,
        ),
        (
            "Botanical garden and lake circuit",
            "Shaded paths, birdlife and a slow loop around the water.",
            ActivityCategory.RELAXATION,
            150,
            Slot.AFTERNOON,
        ),
    ),
    Interest.HISTORY: (
        (
            "{dest} old quarter walking tour",
            "Work through the oldest streets of {dest} with a local guide, ending at the central landmark.",  # noqa: E501
            ActivityCategory.CULTURE,
            180,
            Slot.MORNING,
        ),
        (
            "Fort and museum visit",
            "The defining historic site of {dest}, plus the museum that explains it.",
            ActivityCategory.CULTURE,
            150,
            Slot.MIDDAY,
        ),
        (
            "Heritage architecture trail",
            "Colonial-era and pre-colonial buildings, best photographed in the late afternoon.",
            ActivityCategory.SIGHTSEEING,
            120,
            Slot.AFTERNOON,
        ),
    ),
    Interest.CULTURE: (
        (
            "Local crafts quarter",
            "Workshops and studios where {dest}'s traditional crafts are still made by hand.",
            ActivityCategory.CULTURE,
            120,
            Slot.MIDDAY,
        ),
        (
            "Evening cultural performance",
            "Music or dance local to {dest}. Book ahead; good seats sell out.",
            ActivityCategory.CULTURE,
            120,
            Slot.EVENING,
        ),
        (
            "Neighbourhood market immersion",
            "The everyday market where residents actually shop.",
            ActivityCategory.CULTURE,
            90,
            Slot.MORNING,
        ),
    ),
    Interest.ADVENTURE: (
        (
            "Guided adventure activity",
            "The signature adrenaline activity around {dest} - book with an operator carrying proper insurance.",  # noqa: E501
            ActivityCategory.ADVENTURE,
            240,
            Slot.MORNING,
        ),
        (
            "Cycling route out of {dest}",
            "A half-day ride on quieter roads with a support vehicle option.",
            ActivityCategory.ADVENTURE,
            210,
            Slot.MORNING,
        ),
        (
            "Water sports session",
            "Instructor-led session for beginners, gear included.",
            ActivityCategory.ADVENTURE,
            150,
            Slot.MIDDAY,
        ),
    ),
    Interest.NIGHTLIFE: (
        (
            "Rooftop sundowners",
            "Drinks with a view as {dest} lights up.",
            ActivityCategory.NIGHTLIFE,
            120,
            Slot.EVENING,
        ),
        (
            "Live music night",
            "A venue locals rate rather than the tourist strip.",
            ActivityCategory.NIGHTLIFE,
            180,
            Slot.EVENING,
        ),
        (
            "Night market crawl",
            "Street food and browsing after dark.",
            ActivityCategory.NIGHTLIFE,
            150,
            Slot.EVENING,
        ),
    ),
    Interest.FOOD: (
        (
            "Street food walking tour",
            "Six to eight tastings across {dest}'s best-known food lanes, with a guide who explains each one.",  # noqa: E501
            ActivityCategory.FOOD,
            180,
            Slot.EVENING,
        ),
        (
            "Cooking class",
            "Hands-on class built around a regional dish; you eat what you cook.",
            ActivityCategory.FOOD,
            180,
            Slot.MIDDAY,
        ),
        (
            "Produce market and cafe morning",
            "The wholesale market early, then the city's best coffee.",
            ActivityCategory.FOOD,
            120,
            Slot.MORNING,
        ),
    ),
    Interest.SHOPPING: (
        (
            "Main bazaar and boutiques",
            "The textile and souvenir district of {dest}. Bargain politely and expect to walk.",
            ActivityCategory.SHOPPING,
            150,
            Slot.MIDDAY,
        ),
        (
            "Design and concept stores",
            "Independent labels and homeware worth the baggage allowance.",
            ActivityCategory.SHOPPING,
            120,
            Slot.AFTERNOON,
        ),
    ),
    Interest.SPIRITUAL: (
        (
            "Dawn visit to the principal shrine",
            "Arrive before the crowds; dress modestly and check photography rules.",
            ActivityCategory.CULTURE,
            120,
            Slot.MORNING,
        ),
        (
            "Riverside or hilltop ritual at dusk",
            "The evening ceremony locals attend.",
            ActivityCategory.CULTURE,
            90,
            Slot.EVENING,
        ),
        (
            "Meditation or yoga session",
            "A drop-in session suitable for beginners.",
            ActivityCategory.RELAXATION,
            90,
            Slot.MORNING,
        ),
    ),
    Interest.BEACHES: (
        (
            "Main beach morning",
            "Swim and breakfast before the sun gets heavy. Check the flag warnings.",
            ActivityCategory.RELAXATION,
            180,
            Slot.MORNING,
        ),
        (
            "Quiet cove by road or boat",
            "A less crowded stretch of sand a short ride from {dest}.",
            ActivityCategory.RELAXATION,
            240,
            Slot.MIDDAY,
        ),
        (
            "Sunset point",
            "The spot everyone in {dest} goes to at golden hour - arrive 45 minutes early.",
            ActivityCategory.SIGHTSEEING,
            90,
            Slot.EVENING,
        ),
    ),
    Interest.WILDLIFE: (
        (
            "Early morning safari or nature drive",
            "Wildlife is active at first light. Permits must be arranged in advance.",
            ActivityCategory.ADVENTURE,
            240,
            Slot.MORNING,
        ),
        (
            "Birding walk with a local guide",
            "Resident and migratory species around {dest}.",
            ActivityCategory.SIGHTSEEING,
            150,
            Slot.MORNING,
        ),
        (
            "Conservation centre visit",
            "A reputable, non-interactive facility - avoid anywhere offering animal handling.",
            ActivityCategory.SIGHTSEEING,
            120,
            Slot.AFTERNOON,
        ),
    ),
    Interest.ART: (
        (
            "Principal art museum",
            "The permanent collection plus whatever is on loan this month.",
            ActivityCategory.CULTURE,
            150,
            Slot.MIDDAY,
        ),
        (
            "Gallery district wander",
            "Independent galleries and studio spaces in {dest}.",
            ActivityCategory.CULTURE,
            120,
            Slot.AFTERNOON,
        ),
        (
            "Street art route",
            "Murals and installations, best walked with a map.",
            ActivityCategory.SIGHTSEEING,
            90,
            Slot.MORNING,
        ),
    ),
    Interest.PHOTOGRAPHY: (
        (
            "Golden hour shoot at the signature viewpoint",
            "The postcard shot of {dest}, timed for the light.",
            ActivityCategory.SIGHTSEEING,
            120,
            Slot.EVENING,
        ),
        (
            "Backstreets and portraits walk",
            "Texture, signage and daily life away from the monuments.",
            ActivityCategory.SIGHTSEEING,
            150,
            Slot.MORNING,
        ),
    ),
    Interest.WELLNESS: (
        (
            "Spa or traditional treatment",
            "A regional treatment at a licensed spa.",
            ActivityCategory.RELAXATION,
            120,
            Slot.AFTERNOON,
        ),
        (
            "Sunrise yoga and slow breakfast",
            "A gentle start with no fixed agenda afterwards.",
            ActivityCategory.RELAXATION,
            120,
            Slot.MORNING,
        ),
        (
            "Thermal baths or wellness circuit",
            "Half a day of doing deliberately very little.",
            ActivityCategory.RELAXATION,
            180,
            Slot.AFTERNOON,
        ),
    ),
}

#: Used when the traveller states no interests at all.
DEFAULT_ARCHETYPES: Final[tuple[_Archetype, ...]] = (
    (
        "{dest} highlights orientation walk",
        "The central sights of {dest} in one loop, to get your bearings on day one.",
        ActivityCategory.SIGHTSEEING,
        180,
        Slot.MORNING,
    ),
    (
        "Principal museum or monument",
        "The one site worth paying for if you only pay for one.",
        ActivityCategory.CULTURE,
        150,
        Slot.MIDDAY,
    ),
    (
        "Local market and cafe stop",
        "Where residents shop and eat between errands.",
        ActivityCategory.CULTURE,
        120,
        Slot.AFTERNOON,
    ),
    (
        "Viewpoint at sunset",
        "The best free view in {dest}.",
        ActivityCategory.SIGHTSEEING,
        90,
        Slot.EVENING,
    ),
)

#: Cuisine phrasing and the constraint note for each dietary preference.
DIET: Final[dict[FoodPreference, tuple[str, str]]] = {
    FoodPreference.NO_PREFERENCE: ("regional", "No dietary restriction applied."),
    FoodPreference.VEGETARIAN: (
        "regional vegetarian",
        "Ask for dishes without meat, fish or egg; confirm the stock base in soups.",
    ),
    FoodPreference.VEGAN: (
        "plant-based",
        "Confirm no dairy, ghee, honey or fish sauce - the last is easy to miss "
        "in South-East Asia.",
    ),
    FoodPreference.JAIN: (
        "Jain",
        "No root vegetables, onion or garlic. Call ahead; most kitchens need notice.",
    ),
    FoodPreference.HALAL: (
        "halal",
        "Look for certification rather than assuming; ask about alcohol in marinades.",
    ),
    FoodPreference.KOSHER: (
        "kosher",
        "Kosher kitchens are scarce outside major cities - plan around the ones that exist.",
    ),
    FoodPreference.GLUTEN_FREE: (
        "gluten-free",
        "Watch for soy sauce, thickened gravies and shared fryers.",
    ),
    FoodPreference.SEAFOOD: (
        "seafood-forward",
        "Ask what came in that morning and avoid anything pre-cooked and reheated.",
    ),
}

MEAL_PLAN: Final[tuple[tuple[MealType, str], ...]] = (
    (MealType.BREAKFAST, "Breakfast near the stay"),
    (MealType.LUNCH, "Lunch close to the day's main activity"),
    (MealType.DINNER, "Dinner in a neighbourhood locals eat in"),
)


class HeuristicPlanner:
    """Builds a complete draft itinerary from templates and reference pricing."""

    name = "heuristic-v1"

    def __init__(self, engine: BudgetEngine | None = None) -> None:
        self._engine = engine or BudgetEngine()

    def draft(self, req: TripRequest, envelope: BudgetEnvelope | None = None) -> ItineraryDraft:
        """Produce a draft for ``req``, priced to the given envelope.

        The result is deterministic: the same request always yields the same
        plan, because the RNG is seeded from the request itself.
        """
        env = envelope or self._engine.envelope(req)
        rng = random.Random(self._seed(req))

        book = cd.place_book()
        place = book.resolve(req.destination)
        dest = place.name if place else req.destination
        tier, multiplier = book.tier_for_text(req.destination)
        distance_km, estimated = cd.distance_between(req.origin, req.destination)
        international = cd.is_international(req.origin, req.destination)

        pool = self._pool(req.interests)
        rng.shuffle(pool)

        days = [
            self._build_day(req, env, dest, index, pool, rng) for index in range(req.duration_days)
        ]

        return ItineraryDraft(
            title=f"{req.duration_days} days in {dest}",
            destination_overview=self._overview(dest, tier, req),
            best_time_note=self._season_note(req, dest, estimated),
            food_scene_note=self._food_note(dest, req.food_preference),
            outbound=self._leg(req, dest, distance_km, outbound=True),
            inbound=self._leg(req, dest, distance_km, outbound=False),
            days=days,
            misc_cost_per_person=round(
                cd.from_inr(cd.misc_per_person(multiplier, international), req.currency), 2
            ),
            misc_note=(
                "Visa, travel insurance, forex markup, a local SIM, tips and a small buffer."
                if international
                else "Local SIM top-up, tips, laundry and a small buffer."
            ),
            packing_list=self._packing(req, international),
            travel_tips=self._tips(req, dest, international),
            warnings=self._warnings(req, dest, distance_km, international, estimated),
        )

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    @staticmethod
    def _seed(req: TripRequest) -> int:
        """Stable integer seed derived from the request's identity."""
        payload = "|".join(
            [
                req.origin.casefold(),
                req.destination.casefold(),
                req.start_date.isoformat(),
                req.end_date.isoformat(),
                str(req.travellers),
                req.stay_type.value,
                req.pace.value,
                ",".join(sorted(i.value for i in req.interests)),
            ]
        )
        return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")

    @staticmethod
    def _pool(interests: list[Interest]) -> list[_Archetype]:
        """Flatten the archetypes for the chosen interests into one pool."""
        if not interests:
            return list(DEFAULT_ARCHETYPES)
        pool: list[_Archetype] = []
        for interest in interests:
            pool.extend(ARCHETYPES.get(interest, ()))
        return pool or list(DEFAULT_ARCHETYPES)

    def _build_day(
        self,
        req: TripRequest,
        env: BudgetEnvelope,
        dest: str,
        index: int,
        pool: list[_Archetype],
        rng: random.Random,
    ) -> DraftDay:
        first, last = index == 0, index == req.duration_days - 1
        count = ACTIVITIES_PER_DAY.get(req.pace, 3)
        if first:
            # An afternoon arrival loses most of the day to the transfer.
            count = max(count - 1, 1)
        if last and not first:
            # On a checkout day you realistically fit one thing in before you go.
            count = 1

        chosen = self._select(pool, index, count, first=first, last=last)
        per_activity = env.activities_per_person_per_day / count if count else 0.0

        activities: list[DraftActivity] = []
        floors: list[int] = []
        if first:
            activities.append(self._arrival_activity(dest))
            floors.append(ARRIVAL_DAY_START)

        for title, description, category, minutes, slot in chosen:
            floors.append(SLOT_EARLIEST[slot])
            free = category in (ActivityCategory.RELAXATION, ActivityCategory.SIGHTSEEING) and (
                rng.random() < 0.35
            )
            activities.append(
                DraftActivity(
                    start_time="",  # assigned below, once the order is final
                    title=title.format(dest=dest),
                    description=description.format(dest=dest),
                    category=category,
                    duration_minutes=minutes,
                    location=dest,
                    estimated_cost_per_person=0.0 if free else round(per_activity, 2),
                    booking_tip=(
                        "No ticket needed - go early to avoid crowds."
                        if free
                        else "Book online a day ahead; walk-up prices are usually higher."
                    ),
                )
            )

        if last:
            activities.append(self._departure_activity(dest))
            # No floor of its own: checkout simply follows whatever came before.
            floors.append(0)

        activities, floors = activities[:8], floors[:8]
        self._assign_times(activities, floors, first=first, last=last)

        cuisine, note = DIET.get(req.food_preference, DIET[FoodPreference.NO_PREFERENCE])
        meals_today = MEAL_PLAN if not first else MEAL_PLAN[1:]
        per_meal = env.food_per_person_per_day / max(len(meals_today), 1)
        meals = [
            DraftMeal(
                meal_type=meal_type,
                suggestion=f"{label} - {cuisine} cooking",
                cuisine=cuisine,
                location=dest,
                dietary_note=note,
                estimated_cost_per_person=round(per_meal, 2),
            )
            for meal_type, label in meals_today
        ]

        return DraftDay(
            day_number=index + 1,
            title=self._day_title(dest, index, first, last),
            theme=self._day_theme(req, index),
            activities=activities[:8],
            meals=meals,
            stay=DraftStay(
                name=f"{_stay_label(req.stay_type)} in central {dest}",
                stay_type=req.stay_type,
                area=f"Central {dest}, within walking distance of transport",
                why=(
                    "A central base cuts daily travel time, which matters more than a "
                    "marginally cheaper room further out."
                ),
                estimated_cost_per_night_total=round(env.stay_per_night, 2),
                booking_tip="Filter for free cancellation and read the three most recent reviews.",
            ),
            local_transport_note=(
                "Mix of walking, metro or shared autos; agree fares before boarding where "
                "meters are not used."
            ),
            local_transport_cost_per_person=round(env.local_transport_per_person_per_day, 2),
        )

    @staticmethod
    def _arrival_activity(dest: str) -> DraftActivity:
        return DraftActivity(
            start_time="",
            title=f"Arrive in {dest} and settle in",
            description=(
                f"Transfer to the stay, drop bags and take an unhurried first walk around "
                f"the immediate area of {dest}. Pick up water and a local SIM."
            ),
            category=ActivityCategory.TRANSIT,
            duration_minutes=120,
            location=dest,
            estimated_cost_per_person=0.0,
            booking_tip="Pre-book the airport or station transfer; arrival-hall rates are worse.",
        )

    @staticmethod
    def _departure_activity(dest: str) -> DraftActivity:
        return DraftActivity(
            start_time="",
            title="Check out and head for the return journey",
            description=(
                "Leave buffer for traffic and security. Keep a change of clothes and "
                "essentials in hand baggage."
            ),
            category=ActivityCategory.TRANSIT,
            duration_minutes=120,
            location=dest,
            estimated_cost_per_person=0.0,
            booking_tip="Confirm the return booking 24 hours ahead and check the terminal.",
        )

    @staticmethod
    def _select(
        pool: list[_Archetype], index: int, count: int, *, first: bool, last: bool
    ) -> list[_Archetype]:
        """Choose this day's archetypes, excluding ones the day cannot fit.

        A departure morning has no room for an evening performance, and an
        afternoon arrival has none for a dawn market. Where filtering would
        leave nothing, the unfiltered pool is used rather than returning an
        empty day.
        """
        if last and not first:
            # Short and early only - anything else runs past the checkout.
            allowed = [a for a in pool if a[4] is Slot.MORNING and a[3] <= 180]
        elif first and not last:
            allowed = [a for a in pool if a[4] in (Slot.AFTERNOON, Slot.EVENING)]
        else:
            allowed = list(pool)
        allowed = allowed or list(pool)

        picked = [allowed[(index * count + n) % len(allowed)] for n in range(count)]
        return sorted(picked, key=lambda archetype: archetype[4])

    @staticmethod
    def _assign_times(
        activities: list[DraftActivity], floors: list[int], *, first: bool, last: bool
    ) -> None:
        """Lay the day out on a clock.

        Walks the activities in order carrying a cursor, and starts each one at
        the later of the cursor and the activity's own earliest sensible time.
        That keeps the day both chronological and semantically sane - a sunset
        viewpoint cannot start before evening, and nothing overlaps.
        """
        cursor = ARRIVAL_DAY_START if first else DEPARTURE_DAY_START if last else DAY_START
        for activity, floor in zip(activities, floors, strict=False):
            start = min(max(cursor, floor), LATEST_START)
            activity.start_time = f"{start // 60:02d}:{start % 60:02d}"
            cursor = start + activity.duration_minutes + TRANSITION_MINUTES

    @staticmethod
    def _day_title(dest: str, index: int, first: bool, last: bool) -> str:
        if first:
            return f"Arrival in {dest}"
        if last:
            return f"Last morning in {dest} and departure"
        return f"Day {index + 1} in {dest}"

    @staticmethod
    def _day_theme(req: TripRequest, index: int) -> str:
        if not req.interests:
            return "Highlights and orientation"
        interest = req.interests[index % len(req.interests)]
        return interest.value.replace("_", " ").title()

    def _leg(
        self, req: TripRequest, dest: str, distance_km: float, *, outbound: bool
    ) -> DraftTransportLeg:
        mode = req.primary_transport()
        rate = cd.TRANSPORT_RATES.get(mode, cd.TRANSPORT_RATES[TransportMode.FLIGHT])
        per_person_inr = rate.fare(distance_km)
        if not rate.per_person:
            # The draft field is per person by contract; for a hired vehicle the
            # whole-party fare is reported once and the engine does not multiply.
            per_person_inr = rate.fare(distance_km)

        src, dst = (req.origin, dest) if outbound else (dest, req.origin)
        hours = _travel_hours(mode, distance_km)
        return DraftTransportLeg(
            mode=mode,
            from_place=src,
            to_place=dst,
            depart_hint="Morning departure" if outbound else "Afternoon departure",
            duration_hint=f"about {hours}",
            description=(
                f"{mode.value.title()} from {src} to {dst}, roughly "
                f"{int(distance_km * rate.detour):,} km of routed distance."
            ),
            estimated_cost_per_person=round(cd.from_inr(per_person_inr, req.currency), 2),
            booking_tip=(
                "Fares are usually lowest three to six weeks out; mid-week departures "
                "beat weekends."
                if mode is TransportMode.FLIGHT
                else "Book as soon as the reservation window opens - quotas fill quickly."
            ),
        )

    @staticmethod
    def _overview(dest: str, tier: str, req: TripRequest) -> str:
        band = tier.replace("_", " ")
        return (
            f"{dest} sits in the {band} price band, so a {req.stay_type.value.replace('_', ' ')} "
            f"base and {req.pace.value} pace is a realistic combination for "
            f"{req.travellers} traveller(s) over {req.duration_days} days. This plan keeps the "
            "stay central and groups activities geographically so you spend time at places "
            "rather than between them."
        )

    @staticmethod
    def _season_note(req: TripRequest, dest: str, estimated: bool) -> str:
        month = req.start_date.strftime("%B")
        caveat = (
            " Distances here are approximate because the route could not be matched to the "
            "reference dataset."
            if estimated
            else ""
        )
        return (
            f"You are travelling in {month}. Check {dest}'s monsoon, peak-season and public "
            f"holiday calendar before booking: the same room can double in peak weeks.{caveat}"
        )

    @staticmethod
    def _food_note(dest: str, preference: FoodPreference) -> str:
        cuisine, note = DIET.get(preference, DIET[FoodPreference.NO_PREFERENCE])
        return (
            f"{dest} is workable on a {cuisine} diet with a little planning. {note} "
            "Learning the phrase for your restriction in the local language helps more "
            "than any app."
        )

    @staticmethod
    def _packing(req: TripRequest, international: bool) -> list[str]:
        items = [
            "Photo ID and printed copies of every booking",
            "Universal power adapter and a power bank",
            "Refillable water bottle",
            "Basic medical kit: rehydration salts, painkillers, antiseptic, plasters",
            "Prescription medicines in original packaging with the prescription",
            "Light layers plus one warm layer for air-conditioning and evenings",
            "Comfortable broken-in walking shoes",
            "Sunscreen, sunglasses and a hat",
        ]
        if international:
            items[:0] = [
                "Passport valid at least six months beyond the return date",
                "Visa or e-visa approval printout",
                "Travel insurance policy number and 24-hour helpline",
                "Two payment cards enabled for international use, kept separately",
            ]
        if req.children:
            items.append("Snacks, entertainment and spare clothes for the children")
        if req.accessibility_needs:
            items.append(
                "Accessibility equipment plus written confirmation of assistance requested"
            )
        if req.stay_type == StayType.HOSTEL:
            items.extend(
                [
                    "Padlock for the locker",
                    "Flip-flops for shared bathrooms",
                    "Earplugs and eye mask",
                ]
            )
        return items[:25]

    @staticmethod
    def _tips(req: TripRequest, dest: str, international: bool) -> list[str]:
        tips = [
            f"Download an offline map of {dest} before you land.",
            "Keep a day's cash in small notes; many small vendors will not take cards.",
            "Screenshot your bookings - connectivity at arrival is unreliable.",
            "Agree the fare before getting into any vehicle without a meter.",
            "Build in one unplanned afternoon; the best parts of a trip rarely come from the plan.",
        ]
        if international:
            tips.extend(
                [
                    "Tell your bank the travel dates so cards are not blocked mid-trip.",
                    "Compare an eSIM against local SIM pricing before you travel.",
                ]
            )
        if req.pace is Pace.PACKED:
            tips.append(
                "This is a dense schedule - drop one activity a day rather than "
                "rushing all of them."
            )
        if req.accessibility_needs:
            tips.append(
                "Confirm step-free access directly with each venue; listings are often optimistic."
            )
        return tips[:15]

    @staticmethod
    def _warnings(
        req: TripRequest,
        dest: str,
        distance_km: float,
        international: bool,
        estimated: bool,
    ) -> list[str]:
        out = [
            "Costs here are estimates from reference data, not live quotes - "
            "verify before booking.",
            f"Check the current travel advisory and entry rules for {dest} "
            "before paying for anything.",
        ]
        if international:
            out.append("Confirm visa processing time; some e-visas still take two weeks.")
        if estimated:
            out.append(
                "The route could not be matched to a known city pair, so the travel cost is a "
                "rough estimate."
            )
        mode = req.primary_transport()
        if not cd.mode_plausible(mode, distance_km, international):
            out.append(
                f"Travelling by {mode.value} over roughly {int(distance_km):,} km may not be "
                "practical on this route - a flight is likely the only realistic option."
            )
        return out[:10]


def _stay_label(stay_type: StayType) -> str:
    return stay_type.value.replace("_", " ").title()


def _travel_hours(mode: TransportMode, distance_km: float) -> str:
    """Rough door-to-door duration string for a leg."""
    speeds = {
        TransportMode.FLIGHT: 700.0,
        TransportMode.TRAIN: 60.0,
        TransportMode.BUS: 45.0,
        TransportMode.CAR: 50.0,
        TransportMode.FERRY: 35.0,
    }
    overheads = {TransportMode.FLIGHT: 3.5}
    speed = speeds.get(mode, 60.0)
    hours = distance_km / speed + overheads.get(mode, 0.5)
    if hours < 1:
        return "under an hour"
    whole = int(hours)
    minutes = int(round((hours - whole) * 60 / 15) * 15)
    if minutes >= 60:
        whole, minutes = whole + 1, 0
    return f"{whole}h {minutes:02d}m" if minutes else f"{whole}h"
