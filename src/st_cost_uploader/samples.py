"""The sample spreadsheets offered from the upload screen.

Both layouts render from one dataset, so the two files cannot describe
different spend, and a parser change that breaks either one breaks the
tests. A sample that no longer parses is worse than no sample at all.
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import UTC, date, datetime

SAMPLE_LAYOUTS = ("long", "wide")
SAMPLE_MONTH_COUNT = 3

# (campaign name, month offset from the first sample month, amount).
#
# "Direct Mail - North" is funded in one month only. That leaves blank cells
# in the wide file, which is how the sample documents that an empty cell is
# skipped rather than read as 0 -- an operator whose campaign ran for part of
# a year needs to know that, and a fully populated sample would not show it.
_SAMPLE_SPEND: tuple[tuple[str, int, str], ...] = (
    ("Spring Radio Push", 0, "4000.00"),
    ("Spring Radio Push", 1, "4000.00"),
    ("Spring Radio Push", 2, "3000.00"),
    ("Direct Mail - North", 1, "1500.00"),
    ("Google Search Brand", 0, "8250.00"),
    ("Google Search Brand", 1, "8250.00"),
    ("Google Search Brand", 2, "9000.00"),
)


def _month_at(start: date, offset: int) -> tuple[int, int]:
    index = start.month - 1 + offset
    return start.year + index // 12, index % 12 + 1


def sample_months(today: date | None = None) -> list[tuple[int, int]]:
    """This month and the two after it.

    Computed rather than hardcoded on purpose. A sample frozen at a past
    month invites an operator to fill in amounts without touching the month
    column and write real spend to the wrong period. The preview gate would
    catch that, but the sample should not set the trap.

    UTC rather than local time, matching audit.py. Within a few hours of a
    month boundary that can name next month instead of this one, which is
    cosmetic here: these periods are a starting point the operator edits,
    not a value the tool acts on.
    """
    start = today or datetime.now(UTC).date()
    return [_month_at(start, offset) for offset in range(SAMPLE_MONTH_COUNT)]


def _long_rows(months: list[tuple[int, int]]) -> list[list[str]]:
    # These three headers are exactly what the parser's column patterns
    # match on, so the sample states what the detector wants rather than
    # approximating it.
    rows = [["Campaign", "Month", "Total Spend"]]
    for name, offset, amount in _SAMPLE_SPEND:
        year, month = months[offset]
        rows.append([name, f"{year}-{month:02d}", f"${amount}"])
    return rows


def _wide_rows(months: list[tuple[int, int]]) -> list[list[str]]:
    # The column headers spell the period as "Nov 2026" while the long file
    # spells it "2026-11". Both parse. Showing two spellings tells the
    # operator the parser is tolerant, so they do not reformat a sheet that
    # already works.
    rows = [["Campaign"] + [f"{calendar.month_abbr[m]} {y}" for y, m in months]]

    by_campaign: dict[str, dict[int, str]] = {}
    for name, offset, amount in _SAMPLE_SPEND:
        by_campaign.setdefault(name, {})[offset] = amount

    for name, amounts in by_campaign.items():
        rows.append(
            [name] + [f"${amounts[i]}" if i in amounts else "" for i in range(len(months))]
        )
    return rows


def sample_csv(layout: str, today: date | None = None) -> str:
    """Render the sample dataset as CSV text in the requested layout."""
    if layout not in SAMPLE_LAYOUTS:
        raise ValueError(
            f"Unknown sample layout {layout!r}. Use one of {', '.join(SAMPLE_LAYOUTS)}."
        )

    months = sample_months(today)
    rows = _long_rows(months) if layout == "long" else _wide_rows(months)

    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()
