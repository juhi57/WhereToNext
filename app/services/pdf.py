"""PDF export, built with ReportLab's Platypus flowables.

The output is a document someone would actually carry: a cover with the verdict
up front, a budget breakdown with proportion bars, the two long-haul legs, then
a page per day, and finally packing, tips and warnings.

Two implementation notes worth knowing before editing this file:

* **Fonts.** Base-14 Helvetica has no rupee glyph, so all money here is
  formatted with ``ascii_safe=True`` (see :func:`app.core.money.format_money`).
  Do not reach for the Unicode symbol; it renders as an empty box.
* **Flowables, not canvas drawing.** Everything is a flowable so Platypus
  handles pagination. The only direct canvas work is the page furniture
  (header rule, page numbers) in :class:`_PageFurniture`.
"""

from __future__ import annotations

import io
from typing import Final

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.core.money import format_money
from app.models.enums import BudgetVerdict, Currency, MealType
from app.models.itinerary import Itinerary, ItineraryDay

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #

INK: Final = colors.HexColor("#1A1A1A")
MUTED: Final = colors.HexColor("#6B6B6B")
RULE: Final = colors.HexColor("#DCDCDC")
ACCENT: Final = colors.HexColor("#0F6B5C")
ACCENT_SOFT: Final = colors.HexColor("#E6F1EE")
BAND: Final = colors.HexColor("#F6F6F4")

VERDICT_COLOURS: Final[dict[BudgetVerdict, colors.Color]] = {
    BudgetVerdict.UNDER_BUDGET: colors.HexColor("#1B7A4B"),
    BudgetVerdict.ON_BUDGET: colors.HexColor("#0F6B5C"),
    BudgetVerdict.NEAR_LIMIT: colors.HexColor("#A86A12"),
    BudgetVerdict.OVER_BUDGET: colors.HexColor("#A5281F"),
}

VERDICT_LABELS: Final[dict[BudgetVerdict, str]] = {
    BudgetVerdict.UNDER_BUDGET: "Comfortably under budget",
    BudgetVerdict.ON_BUDGET: "Within budget",
    BudgetVerdict.NEAR_LIMIT: "At the budget limit",
    BudgetVerdict.OVER_BUDGET: "Over budget",
}

MEAL_ORDER: Final[dict[MealType, int]] = {
    MealType.BREAKFAST: 0,
    MealType.SNACK: 1,
    MealType.LUNCH: 2,
    MealType.DINNER: 3,
}

PAGE_MARGIN: Final = 18 * mm


# --------------------------------------------------------------------------- #
# Custom flowables
# --------------------------------------------------------------------------- #


class ProportionBar(Flowable):
    """A single horizontal bar showing one category's share of the total."""

    def __init__(self, fraction: float, width: float, height: float = 4.5 * mm) -> None:
        super().__init__()
        self.fraction = min(max(fraction, 0.0), 1.0)
        self.width = width
        self.height = height

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFillColor(ACCENT_SOFT)
        canvas.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        if self.fraction > 0:
            canvas.setFillColor(ACCENT)
            canvas.rect(0, 0, self.width * self.fraction, self.height, stroke=0, fill=1)


class VerdictBadge(Flowable):
    """A filled pill stating whether the plan fits the budget."""

    def __init__(self, text: str, colour: colors.Color, width: float) -> None:
        super().__init__()
        self.text = text
        self.colour = colour
        self.width = width
        self.height = 11 * mm

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFillColor(self.colour)
        canvas.roundRect(0, 0, self.width, self.height, 2.5 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawCentredString(self.width / 2, self.height / 2 - 3.8, self.text.upper())


class HorizontalRule(Flowable):
    """A thin full-width rule used to separate sections."""

    def __init__(self, width: float, colour: colors.Color = RULE, thickness: float = 0.6) -> None:
        super().__init__()
        self.width = width
        self.height = thickness
        self.colour = colour

    def draw(self) -> None:
        self.canv.setStrokeColor(self.colour)
        self.canv.setLineWidth(self.height)
        self.canv.line(0, 0, self.width, 0)


class _PageFurniture:
    """Draws the running header and footer on every page but the cover."""

    def __init__(self, title: str) -> None:
        self.title = title

    def __call__(self, canvas: Canvas, doc: BaseDocTemplate) -> None:
        canvas.saveState()
        width, height = A4

        if doc.page > 1:
            canvas.setFont("Helvetica", 7.5)
            canvas.setFillColor(MUTED)
            canvas.drawString(PAGE_MARGIN, height - PAGE_MARGIN + 4 * mm, self.title[:88])
            canvas.setStrokeColor(RULE)
            canvas.setLineWidth(0.5)
            canvas.line(
                PAGE_MARGIN,
                height - PAGE_MARGIN + 2.5 * mm,
                width - PAGE_MARGIN,
                height - PAGE_MARGIN + 2.5 * mm,
            )

        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            PAGE_MARGIN,
            PAGE_MARGIN - 6 * mm,
            "Estimates from reference pricing - verify before booking.",
        )
        canvas.drawRightString(width - PAGE_MARGIN, PAGE_MARGIN - 6 * mm, f"Page {doc.page}")
        canvas.restoreState()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


class ItineraryPdfBuilder:
    """Renders an :class:`~app.models.itinerary.Itinerary` to PDF bytes."""

    def __init__(self, page_size: tuple[float, float] = A4) -> None:
        self.page_size = page_size
        self.content_width = page_size[0] - 2 * PAGE_MARGIN
        self._styles = self._build_styles()

    def build(self, itinerary: Itinerary) -> bytes:
        """Return the complete PDF as bytes."""
        buffer = io.BytesIO()
        doc = BaseDocTemplate(
            buffer,
            pagesize=self.page_size,
            leftMargin=PAGE_MARGIN,
            rightMargin=PAGE_MARGIN,
            topMargin=PAGE_MARGIN,
            bottomMargin=PAGE_MARGIN,
            title=itinerary.title,
            author="WhereToNext",
            subject=(
                f"Travel itinerary: {itinerary.request.origin} to {itinerary.request.destination}"
            ),
            creator="WhereToNext itinerary planner",
        )
        frame = Frame(
            PAGE_MARGIN,
            PAGE_MARGIN,
            self.content_width,
            self.page_size[1] - 2 * PAGE_MARGIN,
            id="body",
            showBoundary=0,
        )
        furniture = _PageFurniture(itinerary.title)
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=furniture)])

        story: list[Flowable] = []
        story += self._cover(itinerary)
        story.append(PageBreak())
        story += self._budget_section(itinerary)
        story += self._transport_section(itinerary)
        story.append(PageBreak())
        story += self._days_section(itinerary)
        story += self._closing_section(itinerary)

        doc.build(story)
        return buffer.getvalue()

    # ----------------------------------------------------------------- #
    # Styles
    # ----------------------------------------------------------------- #

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()["BodyText"]
        mk = lambda **kw: ParagraphStyle(parent=base, **kw)  # noqa: E731
        return {
            "cover_title": mk(
                name="cover_title",
                fontName="Helvetica-Bold",
                fontSize=27,
                leading=32,
                textColor=INK,
                spaceAfter=4,
            ),
            "cover_sub": mk(
                name="cover_sub",
                fontName="Helvetica",
                fontSize=12.5,
                leading=17,
                textColor=MUTED,
            ),
            "h1": mk(
                name="h1",
                fontName="Helvetica-Bold",
                fontSize=15,
                leading=19,
                textColor=INK,
                spaceBefore=4,
                spaceAfter=6,
            ),
            "h2": mk(
                name="h2",
                fontName="Helvetica-Bold",
                fontSize=11.5,
                leading=15,
                textColor=ACCENT,
                spaceBefore=8,
                spaceAfter=3,
            ),
            "day_title": mk(
                name="day_title",
                fontName="Helvetica-Bold",
                fontSize=13,
                leading=16,
                textColor=INK,
            ),
            "day_meta": mk(
                name="day_meta",
                fontName="Helvetica",
                fontSize=8.5,
                leading=12,
                textColor=MUTED,
            ),
            "body": mk(
                name="body", fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=INK
            ),
            "body_muted": mk(
                name="body_muted", fontName="Helvetica", fontSize=8.5, leading=12, textColor=MUTED
            ),
            "cell": mk(
                name="cell", fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=INK
            ),
            "cell_bold": mk(
                name="cell_bold",
                fontName="Helvetica-Bold",
                fontSize=8.5,
                leading=11.5,
                textColor=INK,
            ),
            "cell_muted": mk(
                name="cell_muted", fontName="Helvetica", fontSize=7.8, leading=10.5, textColor=MUTED
            ),
            "cell_right": mk(
                name="cell_right",
                fontName="Helvetica",
                fontSize=8.5,
                leading=11.5,
                textColor=INK,
                alignment=TA_RIGHT,
            ),
            "bullet": mk(
                name="bullet",
                fontName="Helvetica",
                fontSize=9,
                leading=13,
                textColor=INK,
                leftIndent=10,
                bulletIndent=1,
                spaceAfter=1.5,
            ),
            "centre_muted": mk(
                name="centre_muted",
                fontName="Helvetica",
                fontSize=8.5,
                leading=12,
                textColor=MUTED,
                alignment=TA_CENTER,
            ),
        }

    # ----------------------------------------------------------------- #
    # Sections
    # ----------------------------------------------------------------- #

    def _cover(self, it: Itinerary) -> list[Flowable]:
        req, budget = it.request, it.budget
        cur = budget.currency
        s = self._styles
        out: list[Flowable] = [Spacer(1, 22 * mm)]

        out.append(Paragraph(_esc(it.title), s["cover_title"]))
        out.append(Paragraph(f"{_esc(req.origin)} &rarr; {_esc(req.destination)}", s["cover_sub"]))
        out.append(Spacer(1, 3 * mm))
        out.append(
            Paragraph(
                f"{req.start_date.strftime('%d %B %Y')} to {req.end_date.strftime('%d %B %Y')}"
                f" &middot; {req.duration_days} days, {req.nights} nights"
                f" &middot; {_travellers(req.adults, req.children)}",
                s["cover_sub"],
            )
        )
        out.append(Spacer(1, 9 * mm))
        out.append(HorizontalRule(self.content_width))
        out.append(Spacer(1, 9 * mm))

        colour = VERDICT_COLOURS.get(budget.verdict, ACCENT)
        out.append(VerdictBadge(VERDICT_LABELS.get(budget.verdict, "Budget"), colour, 58 * mm))
        out.append(Spacer(1, 7 * mm))

        headline = [
            ["Estimated total", _m(budget.total, cur)],
            ["Your budget", _m(budget.budget_total, cur)],
            [
                "Difference",
                ("+" if budget.difference >= 0 else "-") + _m(abs(budget.difference), cur),
            ],
            ["Per person", _m(budget.per_person, cur)],
            ["Per person, per day", _m(budget.per_person_per_day, cur)],
        ]
        table = Table(headline, colWidths=[self.content_width * 0.55, self.content_width * 0.45])
        table.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (0, -1), "Helvetica", 10),
                    ("FONT", (1, 0), (1, -1), "Helvetica-Bold", 10),
                    ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
                    ("TEXTCOLOR", (1, 0), (1, -1), INK),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 11),
                    ("TEXTCOLOR", (1, 2), (1, 2), colour),
                ]
            )
        )
        out.append(table)

        if it.destination_overview:
            out.append(Spacer(1, 10 * mm))
            out.append(Paragraph("About this trip", s["h2"]))
            out.append(Paragraph(_esc(it.destination_overview), s["body"]))

        if it.best_time_note:
            out.append(Spacer(1, 4 * mm))
            out.append(Paragraph("Timing", s["h2"]))
            out.append(Paragraph(_esc(it.best_time_note), s["body"]))

        out.append(Spacer(1, 12 * mm))
        out.append(
            Paragraph(
                f"Plan {it.id} &middot; generated {it.created_at.strftime('%d %b %Y %H:%M UTC')}"
                f" &middot; {_mode_label(it)}",
                s["centre_muted"],
            )
        )
        return out

    def _budget_section(self, it: Itinerary) -> list[Flowable]:
        budget, cur, s = it.budget, it.budget.currency, self._styles
        out: list[Flowable] = [
            Paragraph("Budget breakdown", s["h1"]),
            HorizontalRule(self.content_width),
            Spacer(1, 4 * mm),
        ]

        bar_width = self.content_width * 0.22
        rows: list[list[object]] = [
            [
                Paragraph("Category", s["cell_bold"]),
                Paragraph("Amount", s["cell_bold"]),
                Paragraph("Share", s["cell_bold"]),
                "",
            ]
        ]
        for line in budget.lines:
            rows.append(
                [
                    Paragraph(
                        f"<b>{_esc(_category_label(line.category.value))}</b><br/>"
                        f"<font size=7 color='#6B6B6B'>{_esc(line.note)}</font>",
                        s["cell"],
                    ),
                    Paragraph(_m(line.amount, cur), s["cell_right"]),
                    Paragraph(f"{line.share_pct:.1f}%", s["cell_right"]),
                    ProportionBar(line.share_pct / 100.0, bar_width),
                ]
            )
        rows.append(
            [
                Paragraph("<b>Total</b>", s["cell"]),
                Paragraph(f"<b>{_m(budget.total, cur)}</b>", s["cell_right"]),
                Paragraph(f"<b>{budget.utilisation_pct:.0f}%</b>", s["cell_right"]),
                Paragraph("of your budget", s["cell_muted"]),
            ]
        )

        widths = [
            self.content_width * 0.46,
            self.content_width * 0.18,
            self.content_width * 0.12,
            self.content_width * 0.24,
        ]
        table = Table(rows, colWidths=widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BACKGROUND", (0, 0), (-1, 0), BAND),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
                    ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, BAND]),
                ]
            )
        )
        out.append(table)

        if budget.advice:
            out.append(Spacer(1, 6 * mm))
            out.append(Paragraph("What to do about it", s["h2"]))
            out += [Paragraph(_esc(a), s["bullet"], bulletText="•") for a in budget.advice]

        return out

    def _transport_section(self, it: Itinerary) -> list[Flowable]:
        s, cur = self._styles, it.budget.currency
        out: list[Flowable] = [
            Spacer(1, 8 * mm),
            Paragraph("Getting there and back", s["h1"]),
            HorizontalRule(self.content_width),
            Spacer(1, 3 * mm),
        ]

        rows: list[list[object]] = [
            [
                Paragraph("Leg", s["cell_bold"]),
                Paragraph("Route", s["cell_bold"]),
                Paragraph("Timing", s["cell_bold"]),
                Paragraph("Cost", s["cell_bold"]),
            ]
        ]
        for label, leg in (("Outbound", it.outbound), ("Return", it.inbound)):
            rows.append(
                [
                    Paragraph(
                        f"<b>{label}</b><br/>"
                        f"<font size=7 color='#6B6B6B'>{_esc(leg.mode.value.title())}</font>",
                        s["cell"],
                    ),
                    Paragraph(
                        f"{_esc(leg.from_place)} &rarr; {_esc(leg.to_place)}<br/>"
                        f"<font size=7 color='#6B6B6B'>{_esc(leg.description)}</font>",
                        s["cell"],
                    ),
                    Paragraph(
                        f"{_esc(leg.depart_hint)}<br/>"
                        f"<font size=7 color='#6B6B6B'>{_esc(leg.duration_hint)}</font>",
                        s["cell"],
                    ),
                    Paragraph(
                        f"<b>{_m(leg.cost_total, cur)}</b><br/>"
                        f"<font size=7 color='#6B6B6B'>"
                        f"{_m(leg.cost_per_person, cur)} pp</font>"
                        if leg.cost_per_person
                        else f"<b>{_m(leg.cost_total, cur)}</b>",
                        s["cell_right"],
                    ),
                ]
            )

        table = Table(
            rows,
            colWidths=[
                self.content_width * 0.15,
                self.content_width * 0.42,
                self.content_width * 0.23,
                self.content_width * 0.20,
            ],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), BAND),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        out.append(table)

        tips = [leg.booking_tip for leg in (it.outbound, it.inbound) if leg.booking_tip]
        if tips:
            out.append(Spacer(1, 3 * mm))
            for tip in dict.fromkeys(tips):
                out.append(Paragraph(_esc(tip), s["bullet"], bulletText="•"))
        return out

    def _days_section(self, it: Itinerary) -> list[Flowable]:
        s = self._styles
        out: list[Flowable] = [Paragraph("Day by day", s["h1"]), Spacer(1, 2 * mm)]
        for index, day in enumerate(it.days):
            if index:
                out.append(Spacer(1, 7 * mm))
            out += self._day_block(day, it.budget.currency)
        return out

    def _day_block(self, day: ItineraryDay, cur: Currency) -> list[Flowable]:
        s = self._styles
        header = [
            HorizontalRule(self.content_width, ACCENT, 1.2),
            Spacer(1, 2 * mm),
            Paragraph(f"Day {day.day_number} &middot; {_esc(day.title)}", s["day_title"]),
            Paragraph(
                f"{day.day_date.strftime('%A, %d %B %Y')}"
                + (f" &middot; {_esc(day.theme)}" if day.theme else "")
                + f" &middot; day total {_m(day.day_total, cur)}",
                s["day_meta"],
            ),
            Spacer(1, 3 * mm),
        ]

        blocks: list[Flowable] = []
        if day.activities:
            rows: list[list[object]] = []
            for activity in day.activities:
                detail = _esc(activity.description)
                if activity.booking_tip:
                    detail += (
                        f"<br/><font size=7 color='#0F6B5C'>Tip: "
                        f"{_esc(activity.booking_tip)}</font>"
                    )
                rows.append(
                    [
                        Paragraph(
                            f"<b>{_esc(activity.start_time or '-')}</b><br/>"
                            f"<font size=7 color='#6B6B6B'>"
                            f"{_duration(activity.duration_minutes)}</font>",
                            s["cell"],
                        ),
                        Paragraph(
                            f"<b>{_esc(activity.title)}</b>"
                            + (
                                f"<br/><font size=7 color='#6B6B6B'>"
                                f"{_esc(activity.location)}</font>"
                                if activity.location
                                else ""
                            )
                            + f"<br/>{detail}",
                            s["cell"],
                        ),
                        Paragraph(
                            _m(activity.cost_total, cur)
                            if activity.cost_total
                            else "<font color='#1B7A4B'>Free</font>",
                            s["cell_right"],
                        ),
                    ]
                )
            table = Table(
                rows,
                colWidths=[
                    self.content_width * 0.13,
                    self.content_width * 0.72,
                    self.content_width * 0.15,
                ],
            )
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            blocks.append(table)

        if day.meals:
            blocks.append(Spacer(1, 2 * mm))
            blocks.append(Paragraph("Meals", s["h2"]))
            for meal in sorted(day.meals, key=lambda m: MEAL_ORDER.get(m.meal_type, 9)):
                text = f"<b>{meal.meal_type.value.title()}</b> &middot; {_esc(meal.suggestion)}"
                if meal.location:
                    text += f" &middot; {_esc(meal.location)}"
                text += f" &middot; <b>{_m(meal.cost_total, cur)}</b>"
                if meal.dietary_note:
                    text += f"<br/><font size=7 color='#6B6B6B'>{_esc(meal.dietary_note)}</font>"
                blocks.append(Paragraph(text, s["bullet"], bulletText="•"))

        extras: list[str] = []
        if day.stay is not None:
            stay = day.stay
            label = _m(stay.cost_total, cur) if stay.charged else "no charge - departure day"
            extras.append(
                f"<b>Stay</b> &middot; {_esc(stay.name)}"
                + (f" ({_esc(stay.area)})" if stay.area else "")
                + f" &middot; <b>{label}</b>"
                + (f"<br/><font size=7 color='#6B6B6B'>{_esc(stay.why)}</font>" if stay.why else "")
            )
        if day.local_transport_total or day.local_transport_note:
            extras.append(
                f"<b>Local transport</b> &middot; {_esc(day.local_transport_note)}"
                f" &middot; <b>{_m(day.local_transport_total, cur)}</b>"
            )
        if extras:
            blocks.append(Spacer(1, 2 * mm))
            blocks.append(Paragraph("Logistics", s["h2"]))
            blocks += [Paragraph(e, s["bullet"], bulletText="•") for e in extras]

        # Keep the header with at least the first table so a day never starts
        # with an orphaned heading at the foot of a page.
        return [KeepTogether(header + blocks[:1]), *blocks[1:]]

    def _closing_section(self, it: Itinerary) -> list[Flowable]:
        s, cur = self._styles, it.budget.currency
        out: list[Flowable] = [PageBreak()]

        if it.food_scene_note:
            out.append(Paragraph("Eating well here", s["h1"]))
            out.append(HorizontalRule(self.content_width))
            out.append(Spacer(1, 2 * mm))
            out.append(Paragraph(_esc(it.food_scene_note), s["body"]))
            out.append(Spacer(1, 6 * mm))

        if it.misc_total or it.misc_note:
            out.append(Paragraph("Other costs", s["h2"]))
            out.append(
                Paragraph(
                    f"{_esc(it.misc_note)} &middot; <b>{_m(it.misc_total, cur)}</b> total",
                    s["body"],
                )
            )
            out.append(Spacer(1, 6 * mm))

        for title, items in (
            ("Packing list", it.packing_list),
            ("Practical tips", it.travel_tips),
        ):
            if not items:
                continue
            out.append(Paragraph(title, s["h1"]))
            out.append(HorizontalRule(self.content_width))
            out.append(Spacer(1, 2 * mm))
            out += [Paragraph(_esc(i), s["bullet"], bulletText="•") for i in items]
            out.append(Spacer(1, 6 * mm))

        if it.warnings:
            out.append(Paragraph("Before you book", s["h1"]))
            out.append(
                HorizontalRule(self.content_width, VERDICT_COLOURS[BudgetVerdict.OVER_BUDGET])
            )
            out.append(Spacer(1, 2 * mm))
            out += [Paragraph(_esc(w), s["bullet"], bulletText="•") for w in it.warnings]
            out.append(Spacer(1, 6 * mm))

        if it.meta.adjustments:
            out.append(Paragraph("How these numbers were produced", s["h1"]))
            out.append(HorizontalRule(self.content_width))
            out.append(Spacer(1, 2 * mm))
            out.append(
                Paragraph(
                    f"Planned by {_mode_label(it)}. Every cost below was recalculated from "
                    "reference pricing data; the planner's own figures were corrected where "
                    "they were implausible.",
                    s["body_muted"],
                )
            )
            out.append(Spacer(1, 2 * mm))
            out += [Paragraph(_esc(a), s["bullet"], bulletText="•") for a in it.meta.adjustments]

        return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _esc(text: str) -> str:
    """Escape text for ReportLab's mini-HTML paragraph markup."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _m(amount: float, currency: Currency) -> str:
    """Money for the PDF: always ASCII-safe, because Helvetica lacks a rupee glyph."""
    return format_money(amount, currency, ascii_safe=True)


def _travellers(adults: int, children: int) -> str:
    parts = [f"{adults} adult{'s' if adults != 1 else ''}"]
    if children:
        parts.append(f"{children} child{'ren' if children != 1 else ''}")
    return ", ".join(parts)


def _duration(minutes: int) -> str:
    if minutes <= 0:
        return ""
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    return f"{hours}h" if hours else f"{mins}m"


def _category_label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _mode_label(it: Itinerary) -> str:
    if it.meta.mode.value == "ai":
        return f"AI ({it.meta.model})"
    return "reference templates (no AI key configured)"


def render_itinerary_pdf(itinerary: Itinerary) -> bytes:
    """Convenience wrapper: render ``itinerary`` to PDF bytes."""
    return ItineraryPdfBuilder().build(itinerary)
