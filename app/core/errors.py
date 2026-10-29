"""Domain exceptions.

Each carries the HTTP status and a stable ``code`` that the API layer turns into
a uniform JSON error body, so clients can branch on a string instead of parsing
prose.
"""

from __future__ import annotations


class WhereToNextError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_payload(self) -> dict[str, object]:
        """Uniform error body: ``{"error": {...}}``."""
        body: dict[str, object] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}


class ItineraryNotFoundError(WhereToNextError):
    """The requested itinerary id does not exist."""

    status_code = 404
    code = "itinerary_not_found"


class PlanGenerationError(WhereToNextError):
    """Planning failed and no fallback was available."""

    status_code = 502
    code = "plan_generation_failed"


class AIUnavailableError(PlanGenerationError):
    """The model could not be reached or refused the request."""

    code = "ai_unavailable"


class RateLimitedError(WhereToNextError):
    """The caller has exceeded the per-minute planning quota."""

    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InfeasibleTripError(WhereToNextError):
    """The budget cannot cover even the unavoidable costs of the trip."""

    status_code = 422
    code = "trip_infeasible"
