"""Command-line interface.

Three subcommands:

* ``estimate`` - price a trip from reference data, instantly and for free.
* ``plan`` - generate a full itinerary and optionally write the PDF.
* ``serve`` - run the web application.

Built on :mod:`argparse` so the package stays dependency-light, and on the same
services the HTTP API uses - there is no second implementation of anything here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import Settings, get_settings
from app.core.errors import WhereToNextError
from app.core.logging import configure_logging
from app.core.money import format_money
from app.models.enums import Currency, FoodPreference, Interest, Pace, StayType, TransportMode
from app.models.itinerary import Itinerary
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.pdf import render_itinerary_pdf
from app.services.planner_service import PlannerService
from app.services.repository import Database, ItineraryRepository

RULE = "-" * 72


def _stream_supports(text: str) -> bool:
    """Whether stdout's encoding can represent ``text``.

    A default Windows console is cp1252, which has no rupee sign (U+20B9) and
    raises ``UnicodeEncodeError`` on ``print``. Rather than crash - or force a
    codec the terminal cannot actually display - the CLI degrades to the same
    ASCII-safe money format the PDF uses.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


#: Resolved once at import; see :func:`_stream_supports`.
ASCII_ONLY = not _stream_supports("₹…")

ELLIPSIS = "..." if ASCII_ONLY else "…"


def _money(amount: float, currency: Currency) -> str:
    """Format money for the terminal, degrading to ASCII where needed."""
    return format_money(amount, currency, ascii_safe=ASCII_ONLY)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    configure_logging("WARNING")
    try:
        if args.command == "serve":
            return _serve(args)
        request = _request_from(args)
        if args.command == "estimate":
            return _estimate(request)
        return asyncio.run(_plan(request, args))
    except WhereToNextError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"       {exc.detail}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wheretonext",
        description="Plan a trip, cost it honestly, and export it as a PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  wheretonext estimate --from Mumbai --to Bali --start 2026-12-10 "
            "--days 6 --budget 180000\n"
            "  wheretonext plan --from Delhi --to Tokyo --start 2026-04-02 --days 8 \\\n"
            "      --budget 350000 --interests food,culture --pdf tokyo.pdf\n"
            "  wheretonext serve --port 8000\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("estimate", "Cost a trip from reference data (no AI call)."),
        ("plan", "Generate a full day-by-day itinerary."),
    ):
        command = sub.add_parser(name, help=help_text, description=help_text)
        _add_trip_arguments(command)
        if name == "plan":
            command.add_argument(
                "--pdf",
                type=Path,
                metavar="FILE",
                help="Also write the itinerary to this PDF path.",
            )
            command.add_argument(
                "--json",
                action="store_true",
                help="Print the itinerary as JSON instead of formatted text.",
            )

    serve = sub.add_parser("serve", help="Run the web application.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Reload on code changes.")

    return parser


def _add_trip_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="origin", required=True, metavar="CITY")
    parser.add_argument("--to", dest="destination", required=True, metavar="CITY")
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD", help="Departure date.")

    length = parser.add_mutually_exclusive_group(required=True)
    length.add_argument("--end", metavar="YYYY-MM-DD", help="Return date.")
    length.add_argument("--days", type=int, metavar="N", help="Trip length in days.")

    parser.add_argument("--budget", type=float, required=True, metavar="AMOUNT")
    parser.add_argument(
        "--currency", default=Currency.INR.value, choices=[c.value for c in Currency]
    )
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument("--children", type=int, default=0)
    parser.add_argument(
        "--transport",
        default=TransportMode.FLIGHT.value,
        choices=[m.value for m in TransportMode],
    )
    parser.add_argument(
        "--stay", default=StayType.MID_HOTEL.value, choices=[s.value for s in StayType]
    )
    parser.add_argument(
        "--food",
        default=FoodPreference.NO_PREFERENCE.value,
        choices=[f.value for f in FoodPreference],
    )
    parser.add_argument("--pace", default=Pace.BALANCED.value, choices=[p.value for p in Pace])
    parser.add_argument(
        "--interests",
        default="",
        metavar="A,B,C",
        help=f"Comma-separated. Choices: {', '.join(i.value for i in Interest)}",
    )
    parser.add_argument("--notes", default=None)
    parser.add_argument("--accessibility", dest="accessibility_needs", default=None)


def _request_from(args: argparse.Namespace) -> TripRequest:
    """Turn parsed arguments into a validated :class:`TripRequest`."""
    start = _parse_date(args.start, "--start")
    end = _parse_date(args.end, "--end") if args.end else start + timedelta(days=args.days - 1)

    interests = [i.strip() for i in args.interests.split(",") if i.strip()]
    return TripRequest(
        origin=args.origin,
        destination=args.destination,
        start_date=start,
        end_date=end,
        adults=args.adults,
        children=args.children,
        budget_total=args.budget,
        currency=Currency(args.currency),
        transport_modes=[TransportMode(args.transport)],
        stay_type=StayType(args.stay),
        food_preference=FoodPreference(args.food),
        pace=Pace(args.pace),
        interests=[Interest(i) for i in interests],
        notes=args.notes,
        accessibility_needs=args.accessibility_needs,
    )


def _parse_date(value: str, flag: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{flag} must be a date in YYYY-MM-DD form, not {value!r}") from None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _estimate(request: TripRequest) -> int:
    engine = BudgetEngine()
    baseline = engine.baseline(request)
    summary = engine.summarise(request, baseline.as_lines(), baseline)
    currency = request.currency

    print(RULE)
    print(f"{request.origin} to {request.destination}")
    print(
        f"{request.duration_days} days, {request.nights} nights, "
        f"{request.travellers} traveller(s)  |  {baseline.tier.replace('_', ' ')} destination  |  "
        f"{baseline.distance_km:,.0f} km each way"
        + ("  (estimated)" if baseline.distance_estimated else "")
    )
    print(RULE)
    for category, amount in baseline.as_lines().items():
        print(f"  {category.value.replace('_', ' '):<18} {_money(amount, currency):>16}")
    print(RULE)
    print(f"  {'TOTAL':<18} {_money(summary.total, currency):>16}")
    print(f"  {'your budget':<18} {_money(summary.budget_total, currency):>16}")
    print(f"  {'per person':<18} {_money(summary.per_person, currency):>16}")
    print(
        f"\n  {summary.verdict.value.replace('_', ' ').upper()} "
        f"({summary.utilisation_pct:.0f}% of budget)"
    )
    for line in summary.advice:
        print(f"    - {line}")
    return 0 if summary.within_budget else 1


async def _plan(request: TripRequest, args: argparse.Namespace) -> int:
    settings = get_settings()
    database = Database(settings.database_url)
    await database.create_schema()
    try:
        service = PlannerService(settings, ItineraryRepository(database))
        if not service.ai_enabled:
            print(
                "note: no ANTHROPIC_API_KEY set - using the reference template planner.\n",
                file=sys.stderr,
            )
        itinerary = (await service.create_plan(request)).itinerary
    finally:
        await database.dispose()

    if args.json:
        print(itinerary.model_dump_json(indent=2))
    else:
        _print_itinerary(itinerary)

    if args.pdf:
        args.pdf.parent.mkdir(parents=True, exist_ok=True)
        args.pdf.write_bytes(render_itinerary_pdf(itinerary))
        print(f"\nPDF written to {args.pdf.resolve()}")

    return 0 if itinerary.budget.within_budget else 1


def _print_itinerary(itinerary: Itinerary) -> None:
    currency = itinerary.budget.currency
    print(RULE)
    print(itinerary.title)
    print(f"{itinerary.request.origin} to {itinerary.request.destination}")
    print(RULE)

    for day in itinerary.days:
        print(f"\nDay {day.day_number}  {day.day_date:%a %d %b}  {day.title}")
        print(f"  {'':<6}{'-' * 60}")
        for activity in day.activities:
            cost = _money(activity.cost_total, currency) if activity.cost_total else "free"
            print(f"  {activity.start_time:<6}{_fit(activity.title):<46}{cost:>14}")
        for meal in day.meals:
            label = _fit(f"{meal.meal_type.value}: {meal.suggestion}")
            print(f"  {'':<6}{label:<46}{_money(meal.cost_total, currency):>14}")
        if day.stay is not None and day.stay.charged:
            stay_label = _fit(f"stay: {day.stay.name}")
            print(f"  {'':<6}{stay_label:<46}{_money(day.stay.cost_total, currency):>14}")
        print(f"  {'':<6}{'day total':<46}{_money(day.day_total, currency):>14}")

    print(f"\n{RULE}")
    for line in itinerary.budget.lines:
        print(
            f"  {line.category.value.replace('_', ' '):<18}"
            f"{_money(line.amount, currency):>16}  {line.share_pct:5.1f}%"
        )
    print(RULE)
    print(f"  {'TOTAL':<18}{_money(itinerary.budget.total, currency):>16}")
    print(
        f"  {itinerary.budget.verdict.value.replace('_', ' ').upper()} "
        f"({itinerary.budget.utilisation_pct:.0f}% of "
        f"{_money(itinerary.budget.budget_total, currency)})"
    )

    if itinerary.warnings:
        print("\nBefore you book:")
        for warning in itinerary.warnings:
            print(f"  - {warning}")
    print(f"\nPlan id: {itinerary.id}  |  planner: {itinerary.meta.mode.value}")


def _fit(text: str, width: int = 46) -> str:
    """Trim a label so the cost column stays aligned."""
    return text if len(text) <= width else text[: width - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - uvicorn is a hard dependency
        print("error: uvicorn is not installed.", file=sys.stderr)
        return 1

    settings: Settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
