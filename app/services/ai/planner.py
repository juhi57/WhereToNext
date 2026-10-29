"""The Claude-backed planner.

One call, streamed, with the response shape pinned by a JSON Schema generated
from :class:`~app.models.draft.ItineraryDraft`. The SDK validates the reply
against that model, so this module either returns a well-formed draft or raises
:class:`~app.core.errors.AIUnavailableError` - it never returns something
half-parsed for the rest of the app to guess about.

Design notes:

* **Streaming**, because a 30-day itinerary is a large response and a
  non-streaming request with a high ``max_tokens`` risks an HTTP timeout.
* **Structured output** via ``output_format``, not prose parsing, not tool
  calling. There is one right answer shape and the schema enforces it.
* **Prompt caching** on the system prompt, which is identical across requests.
* **Adaptive thinking** is always on for this model family and depth is set by
  ``output_config.effort`` - there is no thinking budget to tune.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic

from app.config import Settings
from app.core.errors import AIUnavailableError
from app.core.logging import get_logger
from app.models.draft import ItineraryDraft
from app.models.trip import TripRequest
from app.services.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from app.services.budget import BudgetEnvelope, TripPricing

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AIResult:
    """A draft plus what the call cost and how long it took."""

    draft: ItineraryDraft
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cache_read_tokens: int = 0


class AIPlanner:
    """Generates itinerary drafts with Claude."""

    name = "claude"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._settings = settings
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> anthropic.AsyncAnthropic:
        key = settings.api_key()
        if not key:
            # Constructing without a key would defer the failure to request time
            # with a confusing message; fail here where the cause is obvious.
            raise AIUnavailableError(
                "No Anthropic API key configured.",
                detail="Set ANTHROPIC_API_KEY to enable AI planning.",
            )
        return anthropic.AsyncAnthropic(
            api_key=key,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )

    async def draft(
        self, req: TripRequest, envelope: BudgetEnvelope, baseline: TripPricing
    ) -> AIResult:
        """Generate one itinerary draft.

        Raises :class:`AIUnavailableError` for anything that makes the result
        unusable - transport failure, refusal, truncation, or a reply that does
        not validate against the schema. The caller decides whether to fall back.
        """
        started = time.perf_counter()
        user_prompt = build_user_prompt(req, envelope, baseline)

        try:
            async with self._client.messages.stream(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.max_tokens,
                # Identical on every request, so it caches. Volatile trip detail
                # lives in the user message, after this breakpoint.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
                output_format=ItineraryDraft,
                output_config={"effort": self._settings.effort},
                metadata={"user_id": f"trip-{req.destination[:32]}"},
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise AIUnavailableError(
                "The configured Anthropic API key was rejected.",
                detail="Check ANTHROPIC_API_KEY.",
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AIUnavailableError(
                "The AI service is rate limited; try again shortly.",
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise AIUnavailableError(
                "The AI service took too long to respond.",
                detail=f"Timeout is {self._settings.request_timeout_seconds:.0f}s.",
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailableError("Could not reach the AI service.") from exc
        except anthropic.APIStatusError as exc:
            # Includes BadRequestError, which in practice means the request shape
            # or the model id is wrong - worth logging loudly.
            logger.error(
                "anthropic_api_error",
                extra={"status": exc.status_code, "model": self._settings.anthropic_model},
            )
            raise AIUnavailableError(
                "The AI service rejected the planning request.",
                detail=f"HTTP {exc.status_code}.",
            ) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        self._guard_stop_reason(message)

        draft = getattr(message, "parsed_output", None)
        if draft is None:
            raise AIUnavailableError(
                "The AI service returned a plan that did not match the expected format.",
            )

        usage = getattr(message, "usage", None)
        result = AIResult(
            draft=draft,
            model=self._settings.anthropic_model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            duration_ms=duration_ms,
        )
        logger.info(
            "ai_plan_generated",
            extra={
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_read_tokens": result.cache_read_tokens,
                "duration_ms": duration_ms,
                "days": len(draft.days),
            },
        )
        return result

    @staticmethod
    def _guard_stop_reason(message: object) -> None:
        """Reject responses that stopped for a reason that invalidates them.

        ``refusal`` carries a ``stop_details`` category; ``max_tokens`` means the
        JSON is truncated and therefore unparseable. Both are failures, and both
        are better surfaced than silently retried.
        """
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise AIUnavailableError(
                "The AI service declined to plan this trip.",
                detail=f"Refusal category: {category}.",
            )
        if stop_reason == "max_tokens":
            raise AIUnavailableError(
                "The plan was cut off before it was complete.",
                detail="Increase WTN_MAX_TOKENS or shorten the trip.",
            )
