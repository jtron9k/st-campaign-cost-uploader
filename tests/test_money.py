from decimal import Decimal

import pytest

from st_cost_uploader.money import convert, days_in_month, parse_money


@pytest.mark.parametrize(
    "year,month,expected",
    [
        (2024, 2, 29),  # leap
        (2026, 2, 28),  # not leap
        (2000, 2, 29),  # divisible by 400
        (1900, 2, 28),  # divisible by 100 but not 400
        (2026, 1, 31),
        (2026, 4, 30),
    ],
)
def test_days_in_month(year, month, expected):
    assert days_in_month(year, month) == expected


def test_five_thousand_over_february_2026_loses_four_cents():
    c = convert(Decimal("5000.00"), 2026, 2)
    assert c.days_in_month == 28
    assert c.daily_cost == Decimal("178.57")
    assert c.reconstructed_total == Decimal("4999.96")
    assert c.residual == Decimal("-0.04")


def test_seven_thousand_over_leap_february_gains_two_cents():
    c = convert(Decimal("7000.00"), 2024, 2)
    assert c.days_in_month == 29
    assert c.daily_cost == Decimal("241.38")
    assert c.reconstructed_total == Decimal("7000.02")
    assert c.residual == Decimal("0.02")


def test_exact_division_leaves_no_residual():
    c = convert(Decimal("1860.00"), 2026, 3)
    assert c.daily_cost == Decimal("60.00")
    assert c.residual == Decimal("0.00")


def test_rounding_is_half_up_not_bankers():
    # Both cases land exactly on a half-cent, where the two rounding modes
    # disagree. This is the guard against forgetting the rounding argument,
    # since Python's Decimal default is ROUND_HALF_EVEN.
    #
    # 0.775 / 31 = 0.025 exactly -> half-up 0.03, half-even 0.02
    assert convert(Decimal("0.775"), 2026, 1).daily_cost == Decimal("0.03")
    # 1.395 / 31 = 0.045 exactly -> half-up 0.05, half-even 0.04
    assert convert(Decimal("1.395"), 2026, 1).daily_cost == Decimal("0.05")


def test_zero_converts_to_zero():
    c = convert(Decimal("0.00"), 2026, 4)
    assert c.daily_cost == Decimal("0.00")
    assert c.residual == Decimal("0.00")


def test_daily_cost_always_has_two_decimal_places():
    c = convert(Decimal("3100.00"), 2026, 3)
    assert c.daily_cost == Decimal("100.00")
    assert str(c.daily_cost) == "100.00"


def test_residual_is_bounded_by_half_a_cent_per_day():
    for month in range(1, 13):
        c = convert(Decimal("12345.67"), 2026, month)
        limit = Decimal("0.005") * c.days_in_month
        assert abs(c.residual) <= limit


def test_convert_rejects_float_input():
    with pytest.raises(TypeError):
        convert(5000.0, 2026, 2)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5000", Decimal("5000")),
        ("5,000.00", Decimal("5000.00")),
        ("$5,000.00", Decimal("5000.00")),
        (" $1,234.56 ", Decimal("1234.56")),
        ("(250.00)", Decimal("-250.00")),
        (1250, Decimal("1250")),
        (1250.5, Decimal("1250.5")),
        (Decimal("99.99"), Decimal("99.99")),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected
