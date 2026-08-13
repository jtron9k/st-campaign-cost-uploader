import json
from decimal import Decimal

from st_cost_uploader.audit import AuditLog
from st_cost_uploader.models import (
    Action,
    CostConversion,
    PlannedWrite,
    WriteOutcome,
)


def _outcome(ok=True, error=None, action=Action.UPDATE, prior="50.00") -> WriteOutcome:
    conv = CostConversion(
        monthly_total=Decimal("1860.00"), days_in_month=31,
        daily_cost=Decimal("60.00"), reconstructed_total=Decimal("1860.00"),
        residual=Decimal("0.00"),
    )
    plan = PlannedWrite(
        campaign_id=3, campaign_name="Yelp", sheet_name="Yelp Ads",
        year=2026, month=3, action=action, conversion=conv,
        cost_id=77, prior_daily_cost=Decimal(prior) if prior else None,
    )
    return WriteOutcome(plan=plan, ok=ok, error=error)


def test_record_writes_one_json_line(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("acme_east", _outcome())

    lines = (tmp_path / "writes.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["tenant"] == "acme_east"
    assert entry["campaign_id"] == 3
    assert entry["campaign_name"] == "Yelp"
    assert entry["sheet_name"] == "Yelp Ads"
    assert entry["year"] == 2026
    assert entry["month"] == 3
    assert entry["action"] == "update"
    assert entry["prior_daily_cost"] == "50.00"
    assert entry["new_daily_cost"] == "60.00"
    assert entry["monthly_total"] == "1860.00"
    assert entry["residual"] == "0.00"
    assert entry["ok"] is True
    assert entry["error"] is None
    assert "timestamp" in entry


def test_money_is_stored_as_a_string_to_survive_json(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert isinstance(entry["new_daily_cost"], str)


def test_appends_rather_than_truncates(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())
    log.record("t", _outcome())

    assert len((tmp_path / "writes.jsonl").read_text().strip().splitlines()) == 2


def test_creates_the_parent_directory(tmp_path):
    log = AuditLog(tmp_path / "nested" / "deeper" / "writes.jsonl")
    log.record("t", _outcome())

    assert (tmp_path / "nested" / "deeper" / "writes.jsonl").exists()


def test_failures_are_recorded_with_the_error(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome(ok=False, error="HTTP 422: validation failed"))

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert entry["ok"] is False
    assert "422" in entry["error"]


def test_create_records_a_null_prior(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome(action=Action.CREATE, prior=None))

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert entry["action"] == "create"
    assert entry["prior_daily_cost"] is None


def test_timestamp_is_utc_iso8601(tmp_path):
    from datetime import datetime

    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    parsed = datetime.fromisoformat(entry["timestamp"])
    assert parsed.tzinfo is not None
