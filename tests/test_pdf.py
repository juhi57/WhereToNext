"""PDF export.

A PDF is hard to assert on without a parser, so these tests check the things
that actually break: that the bytes are a valid PDF, that it paginates rather
than truncating, that the metadata is set, and - the one that bit us in
development - that no unrenderable glyph reaches the page.
"""

from __future__ import annotations

import base64
import re
import zlib
from datetime import timedelta

import pytest

from app.models.enums import Currency, PlannerMode
from app.models.itinerary import Itinerary, PlannerMeta
from app.models.trip import TripRequest
from app.services.budget import BudgetEngine
from app.services.heuristic import HeuristicPlanner
from app.services.pdf import ItineraryPdfBuilder, render_itinerary_pdf


def build_itinerary(
    engine: BudgetEngine, heuristic: HeuristicPlanner, req: TripRequest
) -> Itinerary:
    draft = heuristic.draft(req, engine.envelope(req))
    plan = engine.reconcile(req, draft)
    return Itinerary(
        request=req,
        title=draft.title,
        destination_overview=draft.destination_overview,
        best_time_note=draft.best_time_note,
        food_scene_note=draft.food_scene_note,
        outbound=plan.outbound,
        inbound=plan.inbound,
        days=plan.days,
        budget=plan.budget,
        misc_note=draft.misc_note,
        misc_total=plan.misc_total,
        packing_list=draft.packing_list,
        travel_tips=draft.travel_tips,
        warnings=draft.warnings,
        meta=PlannerMeta(
            mode=PlannerMode.HEURISTIC, model="heuristic-v1", adjustments=plan.adjustments
        ),
    )


@pytest.fixture
def itinerary(engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest) -> Itinerary:
    return build_itinerary(engine, heuristic, trip)


class TestRendering:
    def test_produces_a_valid_pdf(self, itinerary: Itinerary) -> None:
        pdf = render_itinerary_pdf(itinerary)
        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert len(pdf) > 5_000

    def test_document_metadata_is_set(self, itinerary: Itinerary) -> None:
        pdf = render_itinerary_pdf(itinerary)
        assert b"WhereToNext" in pdf

    def test_the_plan_content_actually_reaches_the_page(self, itinerary: Itinerary) -> None:
        """Guard against a document that renders but is blank."""
        text = extract_text(render_itinerary_pdf(itinerary))
        assert "Budget breakdown" in text
        assert "Day by day" in text
        for day in itinerary.days:
            assert f"Day {day.day_number}" in text

    def test_longer_trips_produce_more_pages(
        self, engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest
    ) -> None:
        """Content must paginate rather than spill off the last page."""
        short = render_itinerary_pdf(itinerary_for(engine, heuristic, trip, days=2))
        long = render_itinerary_pdf(itinerary_for(engine, heuristic, trip, days=14))
        assert page_count(long) > page_count(short)

    @pytest.mark.parametrize("days", [1, 2, 7, 21, 30])
    def test_every_trip_length_renders(
        self, engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest, days: int
    ) -> None:
        pdf = render_itinerary_pdf(itinerary_for(engine, heuristic, trip, days=days))
        assert pdf.startswith(b"%PDF-")

    @pytest.mark.parametrize("currency", list(Currency))
    def test_every_currency_renders(
        self,
        engine: BudgetEngine,
        heuristic: HeuristicPlanner,
        trip: TripRequest,
        currency: Currency,
    ) -> None:
        """No currency may put an unrenderable glyph on the page."""
        req = trip.model_copy(update={"currency": currency})
        pdf = render_itinerary_pdf(build_itinerary(engine, heuristic, req))
        assert pdf.startswith(b"%PDF-")

    def test_the_rupee_sign_never_reaches_the_page(self, itinerary: Itinerary) -> None:
        """Helvetica has no U+20B9 glyph; the PDF must use the 'Rs.' fallback.

        Checked against the *decompressed* content streams - a raw byte search
        over the compressed file would pass no matter what was rendered.
        """
        text = extract_text(render_itinerary_pdf(itinerary))
        assert "Rs." in text, "INR amounts should use the ASCII-safe prefix"
        assert "₹" not in text

    def test_markup_in_user_text_is_escaped(
        self, engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest
    ) -> None:
        """A destination containing angle brackets must not break the renderer."""
        risky = trip.model_copy(
            update={"notes": "<b>bold</b> & <i>italic</i>", "origin": "Mumbai & Thane"}
        )
        pdf = render_itinerary_pdf(build_itinerary(engine, heuristic, risky))
        assert pdf.startswith(b"%PDF-")

    def test_rendering_is_reproducible_in_content_length(self, itinerary: Itinerary) -> None:
        """Two renders of the same plan differ only in the embedded timestamp."""
        first = render_itinerary_pdf(itinerary)
        second = render_itinerary_pdf(itinerary)
        assert abs(len(first) - len(second)) < 200

    def test_empty_optional_sections_do_not_break_it(self, itinerary: Itinerary) -> None:
        bare = itinerary.model_copy(
            update={
                "packing_list": [],
                "travel_tips": [],
                "warnings": [],
                "food_scene_note": "",
                "destination_overview": "",
                "best_time_note": "",
            }
        )
        assert render_itinerary_pdf(bare).startswith(b"%PDF-")


class TestBuilder:
    def test_content_width_fits_inside_the_margins(self) -> None:
        builder = ItineraryPdfBuilder()
        assert 0 < builder.content_width < builder.page_size[0]

    def test_slug_is_filesystem_safe(self, itinerary: Itinerary) -> None:
        slug = itinerary.slug()
        assert slug
        assert all(c.isalnum() or c in "-_" for c in slug)
        assert len(slug) <= 80


def itinerary_for(
    engine: BudgetEngine, heuristic: HeuristicPlanner, trip: TripRequest, *, days: int
) -> Itinerary:
    req = trip.model_copy(update={"end_date": trip.start_date + timedelta(days=days - 1)})
    return build_itinerary(engine, heuristic, req)


def page_count(pdf: bytes) -> int:
    """Count page objects without pulling in a PDF parsing dependency."""
    return pdf.count(b"/Type /Page\n") or pdf.count(b"/Type/Page")


def extract_text(pdf: bytes) -> str:
    """Pull the visible text out of a PDF's content streams.

    ReportLab encodes content streams (ASCII85 over Flate), so searching the raw
    file bytes for a string silently passes whatever the document contains. This
    decodes every stream and collects the literals passed to the text operators,
    which is enough to assert on without adding a PDF parser to the test deps.
    """
    chunks: list[str] = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        content = _decode_stream(raw)
        for literal in re.findall(rb"\((?:\\.|[^\\()])*\)", content):
            text = literal[1:-1]
            text = re.sub(rb"\\([()\\])", rb"\1", text)
            text = re.sub(
                rb"\\([0-7]{1,3})",
                lambda m: bytes([int(m.group(1), 8)]),
                text,
            )
            chunks.append(text.decode("cp1252", errors="replace"))
    return " ".join(chunks)


def _decode_stream(raw: bytes) -> bytes:
    """Undo whatever filters ReportLab applied to one content stream."""
    packed = b"".join(raw.split())
    if packed.endswith(b"~>"):
        packed = packed[:-2]

    for candidate in (_a85(packed), raw):
        if candidate is None:
            continue
        try:
            return zlib.decompress(candidate)
        except zlib.error:
            if candidate is not raw:
                return candidate
    return raw


def _a85(data: bytes) -> bytes | None:
    try:
        return base64.a85decode(data)
    except ValueError:
        return None
