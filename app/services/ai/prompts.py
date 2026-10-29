"""Prompt construction for the planning call.

The split matters for cost: :data:`SYSTEM_PROMPT` is byte-for-byte identical on
every request, so it sits behind a cache breakpoint and is charged at the cache
read rate after the first call. Everything that varies per trip goes in the user
message, after that breakpoint.

The rules below are written as constraints rather than suggestions because the
schema already dictates *shape* - the prompt's job is to dictate *quality*, and
to make clear which numbers the model owns (unit prices) and which it must not
touch (totals).
"""

from __future__ import annotations

from app.core.money import SYMBOLS
from app.models.trip import TripRequest
from app.services.budget import BudgetEnvelope, TripPricing

SYSTEM_PROMPT = """\
You are a senior travel planner who has personally organised thousands of trips. \
You produce itineraries that people actually follow, not glossy lists of landmarks.

## What you are given
A trip brief and a set of per-unit spending targets derived from the traveller's \
budget. You cannot look up live prices, so those targets are your price bracket: \
plan the kind of trip that costs roughly that much.

## Hard rules

1. **Never compute a total, subtotal, percentage or grand sum.** You supply unit \
prices only: per person for travel, activities and meals; per night for the whole \
party for accommodation. Arithmetic is done downstream and your totals would be \
discarded anyway.
2. **Money is a bare number in the requested currency.** No symbols, no commas, \
no ranges, no "approx". Round to sensible increments - a 2350 dinner, not 2347.
3. **Every meal must satisfy the stated dietary preference**, and the dietary note \
must say concretely how (which dish, what to ask for, what to avoid). "Vegetarian \
options available" is a failure.
4. **Be specific and verifiable.** Name real neighbourhoods, districts, beaches, \
temples, markets, trails and well-known establishments. "A local restaurant" and \
"a nearby temple" are failures. If you are not confident a specific venue exists, \
name the area and the type of place instead of inventing a name.
5. **Group each day geographically.** A day's activities must be plausibly \
walkable or a short ride apart. Never send a traveller back and forth across a city.
6. **Respect the pace.** Relaxed means two substantial activities a day, balanced \
three, packed four. Do not overfill a relaxed trip.
7. **Day 1 is an arrival day and the last day is a departure day.** Both lose hours \
to transfers, check-in and check-out. Schedule them accordingly.
8. **Accommodation**: give an entry for every day. The final day's entry describes \
the checkout arrangement; it will not be charged for a night.
9. **Warnings must be real and specific to this destination and these dates**: \
permits that take time, sites closed on certain days, monsoon or wildfire season, \
altitude, common local scams, areas to avoid after dark. Generic safety advice is \
a failure.
10. **If the traveller stated accessibility needs, every activity must be \
compatible with them**, and the booking tips must say what to confirm in advance.
11. Write in clear British English. Be concrete and brief. No marketing adjectives, \
no exclamation marks, no emoji.

## Quality bar
An experienced traveller reading your plan should learn at least three things they \
would not have found in the first page of search results: the right time of day for \
a specific site, a transport trick, a neighbourhood that beats the obvious one, a \
booking window that matters.
"""


def build_user_prompt(req: TripRequest, envelope: BudgetEnvelope, baseline: TripPricing) -> str:
    """The per-trip half of the prompt: the brief plus concrete price targets."""
    currency = req.currency.value
    symbol = SYMBOLS.get(req.currency, currency)
    interests = (
        ", ".join(i.value.replace("_", " ") for i in req.interests) or "no stated preference"
    )
    modes = ", ".join(m.value for m in req.transport_modes)

    lines = [
        "# Trip brief",
        "",
        f"- Route: {req.origin} to {req.destination} and back",
        f"- Dates: {req.start_date.isoformat()} to {req.end_date.isoformat()} "
        f"({req.duration_days} days, {req.nights} nights)",
        f"- Travellers: {req.adults} adult(s)"
        + (f", {req.children} child(ren)" if req.children else ""),
        f"- Total budget: {req.budget_total:,.0f} {currency} for the whole party, whole trip",
        f"- Preferred transport for the long legs: {modes}",
        f"- Accommodation type: {req.stay_type.value.replace('_', ' ')}",
        f"- Food preference: {req.food_preference.value.replace('_', ' ')}",
        f"- Pace: {req.pace.value}",
        f"- Interests: {interests}",
    ]
    if req.accessibility_needs:
        lines.append(f"- Accessibility needs: {req.accessibility_needs}")
    if req.notes:
        lines.append(f"- Traveller's own notes: {req.notes}")

    leg_target = _half(envelope.transport_allowance, req.travellers, req.primary_transport().value)
    misc_target = envelope.misc_allowance / max(req.travellers, 1)

    lines += [
        "",
        "# Reference context",
        "",
        f"- Destination price band: {baseline.tier.replace('_', ' ')}",
        f"- Approximate one-way distance: {baseline.distance_km:,.0f} km"
        + (
            " (estimated - the route is not in the reference dataset)"
            if baseline.distance_estimated
            else ""
        ),
        f"- Crosses an international border: {'yes' if baseline.international else 'no'}",
        "",
        f"# Price targets (all in {currency}, symbol {symbol})",
        "",
        "Plan to land near these. They already account for the traveller's budget, "
        "party size and trip length.",
        "",
        f"- Each one-way long-distance leg, per person: about {leg_target:,.0f}",
        f"- Accommodation, whole party, per night: about {envelope.stay_per_night:,.0f}",
        f"- Food, per person per day (all meals combined): about "
        f"{envelope.food_per_person_per_day:,.0f}",
        f"- Paid activities, per person per day: about "
        f"{envelope.activities_per_person_per_day:,.0f}",
        f"- Local transport, per person per day: about "
        f"{envelope.local_transport_per_person_per_day:,.0f}",
        f"- Miscellaneous, per person for the whole trip: about {misc_target:,.0f}",
    ]

    for note in envelope.notes:
        lines.append(f"- Constraint to respect: {note}")

    if not envelope.feasible:
        lines += [
            "",
            "This budget does not realistically cover the trip. Plan the cheapest "
            "credible version anyway, and use the warnings to say plainly what the "
            "traveller must change.",
        ]

    lines += [
        "",
        f"Produce the complete itinerary for all {req.duration_days} days, numbered 1 to "
        f"{req.duration_days}.",
    ]
    return "\n".join(lines)


def _half(transport_allowance: float, travellers: int, mode: str) -> float:
    """Per-person, one-way share of the return transport allowance.

    A hired car is billed per vehicle, so its allowance is not divided by heads.
    """
    per_leg = transport_allowance / 2
    if mode == "car":
        return per_leg
    return per_leg / max(travellers, 1)
