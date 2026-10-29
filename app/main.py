"""Application factory and wiring.

Shared objects are created once in the lifespan handler and attached to
``app.state``; routes reach them through the dependencies in
:mod:`app.api.deps`. :func:`create_app` takes an optional ``Settings`` so tests
can build an isolated app without touching the environment.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import Settings, get_settings
from app.core.errors import RateLimitedError, WhereToNextError
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import SlidingWindowLimiter
from app.services.planner_service import PlannerService
from app.services.repository import Database, ItineraryRepository

logger = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"

DESCRIPTION = """\
An AI-powered travel itinerary planner.

Give it a route, dates, a budget and your preferences, and it returns a
day-by-day plan with a reconciled cost breakdown and a downloadable PDF.

**How the numbers work.** The language model proposes the trip and quotes *unit*
prices. It never computes a total. Every figure is then re-costed against a
reference pricing model, implausible quotes are corrected, and the corrections
are reported back to you on the plan. Without an API key the service falls back
to a deterministic template planner, so every endpoint still works.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared resources on startup and release them on shutdown."""
    settings: Settings = app.state.settings

    database = Database(settings.database_url)
    await database.create_schema()
    repository = ItineraryRepository(database)

    app.state.database = database
    app.state.repository = repository
    app.state.planner = PlannerService(settings, repository)
    app.state.limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    logger.info(
        "startup",
        extra={
            "environment": settings.environment,
            "ai_enabled": app.state.planner.ai_enabled,
            "model": settings.anthropic_model,
        },
    )
    try:
        yield
    finally:
        await database.dispose()
        logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    _register_middleware(app)
    _register_error_handlers(app)

    app.include_router(api_router)
    _register_web(app)
    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Log every request with its duration and tag it with a timing header."""
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        return response


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(WhereToNextError)
    async def domain_error(request: Request, exc: WhereToNextError) -> JSONResponse:
        """Render every domain error in one shape, with a stable ``code``."""
        headers = {}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after)
        if exc.status_code >= 500:
            logger.error("domain_error", extra={"code": exc.code, "path": request.url.path})
        return JSONResponse(status_code=exc.status_code, content=exc.as_payload(), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Flatten pydantic's errors into field/message pairs the UI can show."""
        fields = [
            {
                "field": ".".join(str(part) for part in err["loc"][1:]) or "body",
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "The trip details are not valid.",
                    "fields": fields,
                }
            },
        )


def _register_web(app: FastAPI) -> None:
    """Serve the single-page UI, when it is present."""
    static_dir = WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    index = WEB_DIR / "templates" / "index.html"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        if not index.is_file():
            return HTMLResponse(
                "<h1>WhereToNext</h1><p>API is running. See <a href='/docs'>/docs</a>.</p>"
            )
        return HTMLResponse(index.read_text(encoding="utf-8"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Container-probe endpoint: no dependencies, no database hit."""
        return {"status": "ok"}


app = create_app()
