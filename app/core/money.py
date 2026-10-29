"""Money formatting and rounding.

Costs in this project are *estimates*, so amounts are carried as ``float`` and
normalised at every boundary through :func:`round_money`. Zero-decimal
currencies (INR, JPY) are rounded to whole units because nobody quotes a hotel
at 4,312.47 rupees.
"""

from __future__ import annotations

from app.models.enums import Currency

#: Currencies conventionally quoted without minor units.
ZERO_DECIMAL: frozenset[Currency] = frozenset({Currency.INR, Currency.JPY})

#: Symbols safe in PDF base-14 fonts. ReportLab's Helvetica uses WinAnsi
#: encoding, which covers $, GBP, EUR and YEN but has no glyph for the rupee
#: sign (U+20B9, added to Unicode well after those fonts were designed), so INR
#: falls back to the "Rs." prefix used on Indian invoices rather than rendering
#: an empty box. See :func:`format_money`.
ASCII_SYMBOLS: dict[Currency, str] = {
    Currency.INR: "Rs. ",
    Currency.USD: "$",
    Currency.EUR: "€",
    Currency.GBP: "£",
    Currency.AED: "AED ",
    Currency.SGD: "S$",
    Currency.JPY: "¥",
    Currency.AUD: "A$",
}

SYMBOLS: dict[Currency, str] = {
    Currency.INR: "₹",
    Currency.USD: "$",
    Currency.EUR: "€",
    Currency.GBP: "£",
    Currency.AED: "AED ",
    Currency.SGD: "S$",
    Currency.JPY: "¥",
    Currency.AUD: "A$",
}


def decimals_for(currency: Currency) -> int:
    """Number of decimal places to display for ``currency``."""
    return 0 if currency in ZERO_DECIMAL else 2


def round_money(amount: float, currency: Currency = Currency.INR) -> float:
    """Round ``amount`` to the precision conventional for ``currency``.

    Negative amounts are clamped to zero: a cost line can be free, never
    negative, and a negative estimate always means an upstream bug.
    """
    if amount <= 0:
        return 0.0
    return round(float(amount), decimals_for(currency))


def format_money(
    amount: float, currency: Currency = Currency.INR, *, ascii_safe: bool = False
) -> str:
    """Render ``amount`` with a thousands separator and the currency symbol.

    Set ``ascii_safe`` for output rendered by a font without a rupee glyph -
    notably the PDF export.

    >>> format_money(125000, Currency.INR)
    '₹1,25,000'
    >>> format_money(125000, Currency.INR, ascii_safe=True)
    'Rs. 1,25,000'
    """
    places = decimals_for(currency)
    table = ASCII_SYMBOLS if ascii_safe else SYMBOLS
    symbol = table.get(currency, f"{currency.value} ")
    if currency is Currency.INR:
        return f"{symbol}{_indian_grouping(amount, places)}"
    return f"{symbol}{amount:,.{places}f}"


def _indian_grouping(amount: float, places: int) -> str:
    """Group digits the Indian way: 12,34,567 rather than 1,234,567."""
    text = f"{abs(amount):.{places}f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    sign = "-" if amount < 0 else ""
    return f"{sign}{whole}.{frac}" if places else f"{sign}{whole}"
