# The cost model

This document explains where every number in a WhereToNext itinerary comes
from, what it is worth, and how to replace it with live pricing.

## The one rule

**The language model never computes a total.**

`ItineraryDraft` — the schema the model fills in — has no field for a total, a
subtotal, a percentage or a verdict. It carries only unit prices:

| Draft field | Unit |
|---|---|
| `DraftTransportLeg.estimated_cost_per_person` | one person, one leg |
| `DraftActivity.estimated_cost_per_person` | one person, one activity |
| `DraftMeal.estimated_cost_per_person` | one person, one meal |
| `DraftStay.estimated_cost_per_night_total` | **whole party**, one night |
| `DraftDay.local_transport_cost_per_person` | one person, one day |
| `ItineraryDraft.misc_cost_per_person` | one person, whole trip |

Everything else is computed in [`app/services/budget.py`](../app/services/budget.py),
which is the only module in the codebase allowed to produce a total.

## Three stages

### 1. `baseline()` — reference pricing only

Prices the trip with no model involvement at all. This is the yardstick for
validation, the basis of the savings advice, and what `POST /api/estimate`
returns.

```
total = transport + accommodation + food + activities + local_transport + misc
```

Each component is a base rate in INR, scaled by the destination's price tier and
converted into the requested currency at the boundary.

### 2. `envelope()` — turning a budget into targets

A model told *"keep the trip under ₹1,80,000"* has no idea what that means per
night. A model told *"about ₹6,400 a night, ₹1,900 per person per day on food"*
produces a plan that lands near the budget.

The envelope carves out the least negotiable costs first — you cannot shop
around the distance to Bali — then divides what remains between accommodation,
food, activities and local transport **in the same proportion the baseline
suggests**, so an adventure-heavy trip keeps its adventure budget:

```
reserve      = 6% of budget                 (safety margin)
spendable    = budget - reserve
transport    = min(baseline transport, spendable)
misc         = min(baseline misc, remaining)
scale        = remaining / baseline(flexible categories)
```

A `scale` below 0.6 or above 1.8 adds a note to the prompt telling the model the
budget is tight or generous, so the plan's tone matches its price bracket.

### 3. `reconcile()` — costing the draft

For every unit price the model supplied:

1. **Clamp it** against the reference unit price for that category, tier and
   party size. Bounds are deliberately generous on the high side — a single
   hot-air-balloon ride legitimately costs many times an average activity.
2. **Multiply it out** to a party total, respecting occupancy rules.
3. **Sum** into the six spend buckets and issue a verdict.

Clamp bounds, as multiples of the reference price:

| Category | Lower | Upper |
|---|---|---|
| Accommodation per night | 0.20× | 4.0× |
| Meal | 0.15× | 6.0× |
| Activity | 0× | 8.0× |
| Transport leg | 0.25× | 3.5× |
| Local transport per day | 0× | 6.0× |
| Miscellaneous | 0× | 5.0× |

**Every clamp is reported.** Corrections land in `PlannerMeta.adjustments` and
are shown in the UI under "How these numbers were produced" and in the PDF.
Silently fixing a number the user was shown would be worse than not fixing it.

## Reference data

[`app/data/places.json`](../app/data/places.json) holds 160 destinations with
coordinates, a country and a price tier, plus 68 country-level tiers used when a
city is not listed.

### Price tiers

| Tier | Multiplier | Examples |
|---|---|---|
| `very_cheap` | 0.65 | Kathmandu, Hanoi, Siem Reap |
| `budget` | 0.80 | Bali, Bangkok, Jaipur |
| `moderate` | 1.00 | Mumbai, Istanbul, Lisbon |
| `expensive` | 1.90 | Paris, Dubai, Toronto |
| `very_expensive` | 2.80 | Zurich, Tokyo, Singapore |

Resolution is tried in order: exact name or alias → alias contained in the query
→ token overlap → country tier → `moderate`. An unrecognised destination is
never priced as if it were free.

### Base rates

All in INR at tier multiplier 1.0.

| Item | Rate |
|---|---|
| Hostel (per bed) | 900 / night |
| Guesthouse | 1,600 / night |
| Budget hotel | 2,600 / night |
| Mid hotel | 5,000 / night |
| Resort | 9,500 / night |
| Luxury hotel | 15,000 / night |
| Breakfast / lunch / dinner | 160 / 360 / 460 per person |
| Activities (relaxed / balanced / packed) | 650 / 1,150 / 1,700 per person per day |
| Local transport | 320 per person per day |
| Miscellaneous | 1,600 per person (+4,500 if international) |

Accommodation tier also drags the food budget: a hostel traveller eats at 0.75×
the base rate, a luxury-hotel traveller at 1.8×.

### Transport

Fare is `max(base + per_km × distance × detour, minimum)`.

| Mode | Base | Per km | Detour | Billed |
|---|---|---|---|---|
| Flight | 2,000 | 2.20 | 1.05 | per person |
| Train | 150 | 1.30 | 1.25 | per person |
| Bus | 100 | 1.60 | 1.30 | per person |
| Car | 600 | 13.00 | 1.30 | **per vehicle** |
| Ferry | 400 | 3.00 | 1.15 | per person |

Distance is the great-circle distance between city centroids. The detour factor
converts that into realistic routed distance.

`mode_plausible()` caps each mode by distance and border crossing, so neither
the planner nor the savings advice suggests a bus to Tokyo.

### Sanity check

| Route | Mode | Distance | Party of 2, return |
|---|---|---|---|
| Mumbai → Delhi | flight | 1,148 km | ₹18,608 |
| Mumbai → Goa | train | 411 km | ₹3,273 |
| Mumbai → Bali | flight | 5,572 km | ₹59,481 |
| Delhi → Zurich | flight | 6,154 km | ₹64,863 |

## Currency

Rates are indicative INR-per-unit values in `DEFAULT_FX_INR`, overridable with
the `WTN_FX_RATES` environment variable (a JSON object). A malformed override
falls back to the defaults rather than taking the app down.

INR and JPY are rendered without minor units, and INR uses lakh/crore grouping
(₹12,34,567). The PDF renders INR as `Rs.` because ReportLab's base-14 fonts
have no `U+20B9` glyph and would print an empty box.

## Replacing this with live pricing

The reference model is isolated behind two functions in
[`app/services/cost_data.py`](../app/services/cost_data.py):

- `transport_cost(mode, distance_km, travellers, round_trip)`
- `stay_cost_per_night(stay_type, travellers, multiplier)`

Swap those for calls to a flights/hotels API and nothing else needs to change —
the clamping, reconciliation and advice all read through them. Make them async
and the call sites in `BudgetEngine` become `await`; everything downstream is
unaffected.

## What this model is not

It is **indicative, not live**. It will not tell you that fares to Goa spike
over New Year, that a specific hotel is sold out, or that a festival has tripled
local rates. Every plan says so, in the UI, in the API response and on the PDF:

> Costs here are estimates from reference data, not live quotes — verify before
> booking.
