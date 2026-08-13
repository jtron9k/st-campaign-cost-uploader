"""Append-only record of every write attempted against a production tenant.

Money is serialized as a string so that JSON never rounds it, and so the log
can be read back into Decimal without loss.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from st_cost_uploader.models import WriteOutcome

DEFAULT_PATH = Path("logs") / "writes.jsonl"


class AuditLog:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self._path = Path(path)

    def record(self, tenant: str, outcome: WriteOutcome) -> None:
        self.record_many(tenant, [outcome])

    def record_many(self, tenant: str, outcomes: list[WriteOutcome]) -> None:
        if not outcomes:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat()
        with self._path.open("a", encoding="utf-8") as handle:
            for outcome in outcomes:
                handle.write(json.dumps(self._entry(tenant, outcome, stamp)) + "\n")

    @staticmethod
    def _entry(tenant: str, outcome: WriteOutcome, stamp: str) -> dict:
        plan = outcome.plan
        prior = plan.prior_daily_cost
        return {
            "timestamp": stamp,
            "tenant": tenant,
            "campaign_id": plan.campaign_id,
            "campaign_name": plan.campaign_name,
            "sheet_name": plan.sheet_name,
            "year": plan.year,
            "month": plan.month,
            "action": plan.action.value,
            # For an UPDATE this is the record the plan targeted; for a
            # CREATE it is the id ServiceTitan minted, which only the write
            # itself can supply.
            "cost_id": outcome.cost_id,
            "monthly_total": str(plan.conversion.monthly_total),
            "prior_daily_cost": None if prior is None else str(prior),
            "new_daily_cost": str(plan.conversion.daily_cost),
            "residual": str(plan.conversion.residual),
            "ok": outcome.ok,
            "error": outcome.error,
        }
