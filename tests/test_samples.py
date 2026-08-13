from datetime import date
from decimal import Decimal

import pytest

from st_cost_uploader.parser import parse_workbook
from st_cost_uploader.samples import SAMPLE_LAYOUTS, sample_csv, sample_months

# November crosses a year boundary, so the month arithmetic is exercised
# rather than assumed: Nov 2026, Dec 2026, Jan 2027.
FIXED = date(2026, 11, 15)


def _parsed(layout: str, today: date = FIXED):
    return parse_workbook(sample_csv(layout, today).encode("utf-8"), f"sample-{layout}.csv")


def _spend(result) -> set[tuple[str, int, int, Decimal]]:
    return {(r.campaign_name, r.year, r.month, r.monthly_total) for r in result.rows}


def test_months_start_at_this_month_and_roll_over_the_year():
    assert sample_months(FIXED) == [(2026, 11), (2026, 12), (2027, 1)]


def test_long_sample_is_detected_as_long_and_parses_clean():
    result = _parsed("long")

    assert result.layout == "long"
    assert len(result.rows) == 7
    assert result.errors == ()


def test_wide_sample_is_detected_as_wide_and_parses_clean():
    result = _parsed("wide")

    assert result.layout == "wide"
    assert len(result.rows) == 7
    assert result.errors == ()


def test_both_layouts_describe_the_same_spend():
    """The load-bearing test. The two files are one dataset rendered twice,
    so if they ever disagree the samples are lying to the operator."""
    assert _spend(_parsed("long")) == _spend(_parsed("wide"))


def test_a_blank_cell_is_skipped_rather_than_read_as_zero():
    rows = [r for r in _parsed("wide").rows if r.campaign_name == "Direct Mail - North"]

    assert len(rows) == 1
    assert rows[0].monthly_total == Decimal("1500.00")


def test_long_headers_are_the_ones_the_detector_matches_on():
    assert _parsed("long").column_mapping == {
        "Campaign": "campaign_name",
        "Month": "period",
        "Total Spend": "monthly_total",
    }


def test_every_declared_layout_renders():
    for layout in SAMPLE_LAYOUTS:
        assert sample_csv(layout, FIXED).strip()


def test_an_unknown_layout_is_rejected():
    with pytest.raises(ValueError, match="sideways"):
        sample_csv("sideways", FIXED)
