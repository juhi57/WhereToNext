"""Controlled vocabularies shared by the request, AI draft and final itinerary models.

Every enum is a ``str`` subclass so values serialise to readable JSON and can be
embedded directly in the JSON Schema handed to the model.
"""

from __future__ import annotations

from enum import StrEnum


class TransportMode(StrEnum):
    """How the traveller covers the long intercity legs."""

    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    CAR = "car"
    FERRY = "ferry"


class StayType(StrEnum):
    """Accommodation tier. Drives the per-night cost band in the cost engine."""

    HOSTEL = "hostel"
    GUESTHOUSE = "guesthouse"
    BUDGET_HOTEL = "budget_hotel"
    MID_HOTEL = "mid_hotel"
    LUXURY_HOTEL = "luxury_hotel"
    HOMESTAY = "homestay"
    RESORT = "resort"
    APARTMENT = "apartment"


class FoodPreference(StrEnum):
    """Dietary constraint applied to every meal suggestion."""

    NO_PREFERENCE = "no_preference"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    JAIN = "jain"
    HALAL = "halal"
    KOSHER = "kosher"
    GLUTEN_FREE = "gluten_free"
    SEAFOOD = "seafood"


class Pace(StrEnum):
    """Activity density per day."""

    RELAXED = "relaxed"
    BALANCED = "balanced"
    PACKED = "packed"


class Interest(StrEnum):
    """Themes used to bias activity selection."""

    NATURE = "nature"
    HISTORY = "history"
    CULTURE = "culture"
    ADVENTURE = "adventure"
    NIGHTLIFE = "nightlife"
    FOOD = "food"
    SHOPPING = "shopping"
    SPIRITUAL = "spiritual"
    BEACHES = "beaches"
    WILDLIFE = "wildlife"
    ART = "art"
    PHOTOGRAPHY = "photography"
    WELLNESS = "wellness"


class ActivityCategory(StrEnum):
    """Classification of a single itinerary slot."""

    SIGHTSEEING = "sightseeing"
    ADVENTURE = "adventure"
    FOOD = "food"
    RELAXATION = "relaxation"
    TRANSIT = "transit"
    SHOPPING = "shopping"
    CULTURE = "culture"
    NIGHTLIFE = "nightlife"
    FREE_TIME = "free_time"


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class Currency(StrEnum):
    """Currencies the budget engine can price in."""

    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    AED = "AED"
    SGD = "SGD"
    JPY = "JPY"
    AUD = "AUD"


class CostCategory(StrEnum):
    """The six buckets every rupee/dollar of the plan is assigned to."""

    TRANSPORT = "transport"
    ACCOMMODATION = "accommodation"
    FOOD = "food"
    ACTIVITIES = "activities"
    LOCAL_TRANSPORT = "local_transport"
    MISC = "misc"


class PlannerMode(StrEnum):
    """Which engine produced the plan - surfaced to the user for transparency."""

    AI = "ai"
    HEURISTIC = "heuristic"


class BudgetVerdict(StrEnum):
    """Outcome of comparing the reconciled total against the user's budget."""

    UNDER_BUDGET = "under_budget"
    ON_BUDGET = "on_budget"
    NEAR_LIMIT = "near_limit"
    OVER_BUDGET = "over_budget"
