"""Decide, per resolved row, whether to create, update, or do nothing.

ServiceTitan does not pre-create a cost row for every campaign-month, and it
does pre-create some at 0.0. Both cases exist inside a single tenant, so the
only safe rule is to look up the period before writing.
"""

from __future__ import annotations

from st_cost_uploader.models import (
    Action,
    CostRecord,
    PeriodKey,
    PlannedWrite,
    Resolution,
)
from st_cost_uploader.money import convert


def plan(
    resolutions: list[Resolution],
    existing_costs: dict[PeriodKey, CostRecord],
) -> tuple[list[PlannedWrite], list[Resolution]]:
    """Return (planned writes, unresolved rows).

    A campaign-month appearing twice in one sheet keeps the last occurrence,
    so the batch can never contain two conflicting writes for one record.
    """
    planned: dict[PeriodKey, PlannedWrite] = {}
    unresolved: list[Resolution] = []

    for resolution in resolutions:
        if not resolution.is_resolved:
            unresolved.append(resolution)
            continue

        row = resolution.row
        conversion = convert(row.monthly_total, row.year, row.month)
        key: PeriodKey = (resolution.campaign_id, row.year, row.month)
        existing = existing_costs.get(key)

        if existing is None:
            action, cost_id, prior = Action.CREATE, None, None
        elif existing.daily_cost == conversion.daily_cost:
            action, cost_id, prior = Action.NO_CHANGE, existing.id, existing.daily_cost
        else:
            action, cost_id, prior = Action.UPDATE, existing.id, existing.daily_cost

        planned[key] = PlannedWrite(
            campaign_id=resolution.campaign_id,
            campaign_name=resolution.campaign_name or "",
            sheet_name=row.campaign_name,
            year=row.year,
            month=row.month,
            action=action,
            conversion=conversion,
            cost_id=cost_id,
            prior_daily_cost=prior,
        )

    return list(planned.values()), unresolved
