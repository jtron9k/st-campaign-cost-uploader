"""The money path. Decimal only, never float."""

from __future__ import annotations

import calendar
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from st_cost_uploader.models import CostConversion

CENTS = Decimal("0.01")


def days_in_month(year: int, month: int) -> int:
    """Days in the given month. Leap years come from the standard library."""
    return calendar.monthrange(year, month)[1]


def convert(monthly_total: Decimal, year: int, month: int) -> CostConversion:
    """Convert a monthly total into the daily cost ServiceTitan stores.

    ServiceTitan holds dailyCost to the cent, so the reconstructed monthly
    total will not always equal the input. The residual is returned rather
    than hidden.
    """
    if not isinstance(monthly_total, Decimal):
        raise TypeError(
            f"monthly_total must be Decimal, got {type(monthly_total).__name__}. "
            "Float input would silently corrupt the money path."
        )

    days = days_in_month(year, month)
    daily = (monthly_total / Decimal(days)).quantize(CENTS, rounding=ROUND_HALF_UP)
    reconstructed = (daily * Decimal(days)).quantize(CENTS, rounding=ROUND_HALF_UP)
    residual = (reconstructed - monthly_total).quantize(CENTS, rounding=ROUND_HALF_UP)

    return CostConversion(
        monthly_total=monthly_total,
        days_in_month=days,
        daily_cost=daily,
        reconstructed_total=reconstructed,
        residual=residual,
    )


def _usable_money(result: Decimal) -> Decimal | None:
    """Return the value only when the rest of the money path can carry it.

    Decimal() happily accepts "NaN", "Infinity" and "1e999", none of which are
    money. NaN is the dangerous one: it survives quantize() silently, renders
    as NaN in every preview column, and serializes as a bare NaN literal,
    which is not valid JSON, straight into a write body. Values too large to
    quantize to the cent are rejected with the same test the money path itself
    performs, so nothing reaches convert() that convert() cannot represent.
    """
    if not result.is_finite():
        return None
    try:
        result.quantize(CENTS)
    except InvalidOperation:
        return None
    return result


def parse_money(value: object) -> Decimal | None:
    """Coerce a spreadsheet cell into Decimal. Returns None when not a number.

    Floats are routed through str() so that 1250.5 becomes Decimal("1250.5")
    rather than the binary expansion.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return _usable_money(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _usable_money(Decimal(str(value)))

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("$", "").replace(",", "").replace(" ", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return _usable_money(parsed)
