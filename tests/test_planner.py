from decimal import Decimal

from st_cost_uploader.models import (
    Action,
    CostRecord,
    MatchKind,
    Resolution,
    SheetRow,
)
from st_cost_uploader.planner import plan


def _resolution(name="Yelp", cid=3, year=2026, month=3, total="1860.00", kind=MatchKind.EXACT):
    row = SheetRow(name, year, month, Decimal(total), 2)
    return Resolution(row, kind, cid, name)


def test_absent_record_becomes_create():
    [written], unresolved = plan([_resolution()], {})

    assert written.action is Action.CREATE
    assert written.cost_id is None
    assert written.prior_daily_cost is None
    assert written.conversion.daily_cost == Decimal("60.00")
    assert unresolved == []


def test_existing_record_with_different_value_becomes_update():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("50.00")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.UPDATE
    assert written.cost_id == 77
    assert written.prior_daily_cost == Decimal("50.00")


def test_existing_record_with_the_same_value_becomes_no_change():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("60.00")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.NO_CHANGE
    assert written.cost_id == 77


def test_existing_zero_record_becomes_update_not_create():
    # ServiceTitan pre-creates some rows at 0.0. Those must be updated,
    # never created again.
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("0.0")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.UPDATE
    assert written.cost_id == 77
    assert written.overwrites_nonzero is False


def test_overwrites_nonzero_flags_only_real_replacements():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("50.00")),
    }
    [written], _ = plan([_resolution()], existing)
    assert written.overwrites_nonzero is True


def test_unresolved_rows_are_returned_separately_and_never_planned():
    row = SheetRow("Mystery", 2026, 3, Decimal("100"), 4)
    unresolved_input = Resolution(row, MatchKind.NONE, None, None)

    written, unresolved = plan([_resolution(), unresolved_input], {})

    assert len(written) == 1
    assert len(unresolved) == 1
    assert unresolved[0].row.campaign_name == "Mystery"


def test_leap_year_divisor_is_used():
    [written], _ = plan([_resolution(year=2024, month=2, total="7000.00")], {})

    assert written.conversion.days_in_month == 29
    assert written.conversion.daily_cost == Decimal("241.38")
    assert written.conversion.residual == Decimal("0.02")


def test_later_duplicate_row_wins():
    # The same campaign-month appearing twice in one sheet is a user error,
    # but it must not produce two conflicting writes.
    first = _resolution(total="1000.00")
    second = _resolution(total="1860.00")

    written, _ = plan([first, second], {})

    assert len(written) == 1
    assert written[0].conversion.monthly_total == Decimal("1860.00")


def test_sheet_name_is_preserved_for_the_preview():
    row = SheetRow("Yelp Ads", 2026, 3, Decimal("1860.00"), 2)
    res = Resolution(row, MatchKind.ALIAS, 3, "Yelp")

    [written], _ = plan([res], {})

    assert written.sheet_name == "Yelp Ads"
    assert written.campaign_name == "Yelp"
