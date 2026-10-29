"""Persistence for generated itineraries.

SQLAlchemy 2.0 async ORM over SQLite by default, so the project runs with no
external service. The ``database_url`` setting accepts any async driver, so
pointing at Postgres (``postgresql+asyncpg://...``) needs no code change.

The full itinerary is stored as JSON in one column rather than normalised across
a dozen tables. That is a deliberate choice: an itinerary is always read and
written whole, it is never queried by activity, and the shape is owned by a
Pydantic model that already validates it. The columns alongside the blob exist
purely to make the listing query cheap.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, String, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.errors import ItineraryNotFoundError
from app.core.logging import get_logger
from app.models.itinerary import Itinerary

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for this application's tables."""


class ItineraryRecord(Base):
    """One saved itinerary.

    ``payload`` holds the serialised :class:`~app.models.itinerary.Itinerary`;
    the other columns are denormalised copies used for listing and filtering.
    """

    __tablename__ = "itineraries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    origin: Mapped[str] = mapped_column(String(120))
    destination: Mapped[str] = mapped_column(String(120), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    travellers: Mapped[int] = mapped_column()

    currency: Mapped[str] = mapped_column(String(3))
    total_cost: Mapped[float] = mapped_column(Float)
    budget_total: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(20))
    planner_mode: Mapped[str] = mapped_column(String(16))

    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    @classmethod
    def from_itinerary(cls, itinerary: Itinerary) -> ItineraryRecord:
        """Build a record from a domain object."""
        return cls(
            id=itinerary.id,
            created_at=itinerary.created_at,
            origin=itinerary.request.origin,
            destination=itinerary.request.destination,
            start_date=itinerary.request.start_date,
            end_date=itinerary.request.end_date,
            travellers=itinerary.request.travellers,
            currency=itinerary.budget.currency.value,
            total_cost=itinerary.budget.total,
            budget_total=itinerary.budget.budget_total,
            verdict=itinerary.budget.verdict.value,
            planner_mode=itinerary.meta.mode.value,
            payload=itinerary.model_dump(mode="json"),
        )

    def to_itinerary(self) -> Itinerary:
        """Rehydrate the domain object from the stored payload."""
        return Itinerary.model_validate(self.payload)

    def summary(self) -> dict[str, Any]:
        """Lightweight representation for list endpoints."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "origin": self.origin,
            "destination": self.destination,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "travellers": self.travellers,
            "currency": self.currency,
            "total_cost": self.total_cost,
            "budget_total": self.budget_total,
            "verdict": self.verdict,
            "planner_mode": self.planner_mode,
        }


class Database:
    """Owns the engine and session factory for the application's lifetime."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self._engine: AsyncEngine = create_async_engine(url, echo=echo, future=True)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """A new session. Callers use it as an async context manager."""
        return self._session_factory()

    async def create_schema(self) -> None:
        """Create any missing tables.

        Fine for a single-table schema. A project with evolving columns should
        reach for Alembic migrations instead - see docs/architecture.md.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("schema_ready")

    async def dispose(self) -> None:
        """Close the connection pool on shutdown."""
        await self._engine.dispose()


class ItineraryRepository:
    """Data access for itineraries."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, itinerary: Itinerary) -> Itinerary:
        """Insert or replace an itinerary, returning what was stored."""
        async with self._db.session() as session, session.begin():
            existing = await session.get(ItineraryRecord, itinerary.id)
            if existing is not None:
                await session.delete(existing)
                await session.flush()
            session.add(ItineraryRecord.from_itinerary(itinerary))
        logger.info(
            "itinerary_saved",
            extra={"itinerary_id": itinerary.id, "destination": itinerary.request.destination},
        )
        return itinerary

    async def get(self, itinerary_id: str) -> Itinerary:
        """Fetch one itinerary or raise :class:`ItineraryNotFoundError`."""
        async with self._db.session() as session:
            record = await session.get(ItineraryRecord, itinerary_id)
            if record is None:
                raise ItineraryNotFoundError(f"No itinerary with id {itinerary_id!r}.")
            return record.to_itinerary()

    async def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Most recently created itineraries, newest first."""
        stmt = (
            select(ItineraryRecord)
            .order_by(ItineraryRecord.created_at.desc())
            .limit(max(1, min(limit, 100)))
            .offset(max(0, offset))
        )
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [row.summary() for row in rows]

    async def delete(self, itinerary_id: str) -> None:
        """Remove an itinerary, or raise if it was never there."""
        async with self._db.session() as session, session.begin():
            record = await session.get(ItineraryRecord, itinerary_id)
            if record is None:
                raise ItineraryNotFoundError(f"No itinerary with id {itinerary_id!r}.")
            await session.delete(record)
        logger.info("itinerary_deleted", extra={"itinerary_id": itinerary_id})

    async def count(self) -> int:
        """Total stored itineraries, used by the health endpoint."""
        async with self._db.session() as session:
            rows = (await session.execute(select(ItineraryRecord.id))).scalars().all()
            return len(rows)


def utcnow() -> datetime:
    return datetime.now(UTC)
