"""Application settings, loaded from the environment and ``.env``.

Every setting is prefixed ``WTN_`` so the app never collides with unrelated
environment variables - with one deliberate exception: ``ANTHROPIC_API_KEY``,
which is read under its conventional name so the standard Anthropic environment
variable just works.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="WTN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- App ------------------------------------------------------------ #
    app_name: str = "WhereToNext"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    #: Origins allowed to call the API from a browser. ``["*"]`` in dev only.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # -- Anthropic ------------------------------------------------------ #
    #: Read from ``WTN_ANTHROPIC_API_KEY`` or plain ``ANTHROPIC_API_KEY``.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("WTN_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )

    #: Claude Opus 5.5 is the current default model. Thinking is always adaptive
    #: on this model and cannot be disabled, so depth is controlled by ``effort``.
    anthropic_model: str = "claude-opus-5-5"

    #: ``medium`` is this model's own default; ``high`` buys noticeably better
    #: day sequencing on long trips at roughly proportional token cost.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    #: Streaming is used for generation, so a large ceiling costs nothing extra
    #: unless the model actually needs the room.
    max_tokens: Annotated[int, Field(ge=1024, le=128_000)] = 32_000

    #: Hard wall-clock ceiling on one planning call, in seconds.
    request_timeout_seconds: Annotated[float, Field(gt=0, le=900)] = 240.0

    #: Retries the SDK performs on 429/5xx before giving up.
    max_retries: Annotated[int, Field(ge=0, le=5)] = 2

    #: When the model fails, serve a template plan instead of an error.
    fallback_to_heuristic: bool = True

    #: Force the template planner even when a key is present. Useful for demos
    #: and for keeping CI free of network calls.
    force_heuristic: bool = False

    # -- Storage -------------------------------------------------------- #
    database_url: str = "sqlite+aiosqlite:///./wheretonext.db"

    # -- Limits --------------------------------------------------------- #
    #: Planning requests allowed per client IP per minute. Generation is the
    #: expensive operation, so this is the endpoint worth protecting.
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=600)] = 10

    #: How long an identical brief is served from the in-process cache.
    cache_ttl_seconds: Annotated[int, Field(ge=0, le=86_400)] = 900

    #: Entries held in the plan cache before the oldest are evicted.
    cache_max_entries: Annotated[int, Field(ge=1, le=10_000)] = 256

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string so ``WTN_CORS_ORIGINS`` is ergonomic."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def ai_available(self) -> bool:
        """Whether a live model call is possible and permitted."""
        return bool(self.anthropic_api_key) and not self.force_heuristic

    def api_key(self) -> str | None:
        """The raw API key, or ``None``. Never log the return value."""
        return self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that reading configuration is free at request time; tests clear the
    cache with ``get_settings.cache_clear()``.
    """
    return Settings()
