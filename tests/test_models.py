from decimal import Decimal

from st_cost_uploader.models import (
    Action,
    Campaign,
    CostConversion,
    CostRecord,
    MatchKind,
    PlannedWrite,
    Resolution,
    SheetRow,
)


def test_sheet_row_is_frozen_and_hashable():
    row = SheetRow(
        campaign_name="Yelp", year=2026, month=3,
        monthly_total=Decimal("1860.00"), source_row=2,
    )
    assert {row}
    assert row.monthly_total == Decimal("1860.00")


def test_cost_record_period_key():
    rec = CostRecord(id=1, campaign_id=99, year=2026, month=3, daily_cost=Decimal("60.00"))
    assert rec.period_key == (99, 2026, 3)


def test_planned_write_period_key_matches_cost_record():
    conv = CostConversion(
        monthly_total=Decimal("1860.00"), days_in_month=31,
        daily_cost=Decimal("60.00"), reconstructed_total=Decimal("1860.00"),
        residual=Decimal("0.00"),
    )
    plan = PlannedWrite(
        campaign_id=99, campaign_name="Yelp", sheet_name="Yelp Ads",
        year=2026, month=3, action=Action.CREATE, conversion=conv,
        cost_id=None, prior_daily_cost=None,
    )
    assert plan.period_key == (99, 2026, 3)


def test_resolution_defaults_to_no_candidates():
    row = SheetRow("Yelp", 2026, 3, Decimal("10"), 2)
    res = Resolution(row=row, kind=MatchKind.NONE, campaign_id=None, campaign_name=None)
    assert res.candidates == ()
    assert res.is_resolved is False


def test_resolution_is_resolved_when_campaign_id_present():
    row = SheetRow("Yelp", 2026, 3, Decimal("10"), 2)
    res = Resolution(row=row, kind=MatchKind.EXACT, campaign_id=99, campaign_name="Yelp")
    assert res.is_resolved is True


def test_campaign_construction():
    c = Campaign(id=1, name="Search||Google||Brand", active=True)
    assert c.name.endswith("Brand")
