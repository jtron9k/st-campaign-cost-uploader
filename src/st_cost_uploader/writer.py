"""Execute an approved plan.

Every write is idempotent per campaign-month, so a partial batch is safe to
re-run. That is why a per-row failure records and continues rather than
aborting: stopping would leave the operator with no clean way to resume.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from st_cost_uploader.audit import AuditLog
from st_cost_uploader.models import Action, PlannedWrite, WriteOutcome

MAX_CONCURRENCY = 8


class CostWriter(Protocol):
    async def create_cost(
        self, campaign_id: int, year: int, month: int, daily_cost: object
    ) -> int: ...

    async def update_cost(
        self, cost_id: int, campaign_id: int, year: int, month: int, daily_cost: object
    ) -> None: ...


async def execute(
    plans: list[PlannedWrite],
    client: CostWriter,
    audit: AuditLog,
    tenant: str,
) -> list[WriteOutcome]:
    """Run every CREATE and UPDATE. NO_CHANGE rows are skipped silently.

    Returns one outcome per attempted write, in plan order.
    """
    actionable = [p for p in plans if p.action is not Action.NO_CHANGE]
    if not actionable:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def run(plan: PlannedWrite) -> WriteOutcome:
        async with semaphore:
            try:
                if plan.action is Action.CREATE:
                    created = await client.create_cost(
                        plan.campaign_id, plan.year, plan.month, plan.conversion.daily_cost
                    )
                    # create_cost returns 0 for "no id in the response",
                    # never for a failure -- a real failure raises before
                    # this line. Normalize that to None so the audit log
                    # never shows a 0 where a record id belongs.
                    return WriteOutcome(plan=plan, ok=True, created_cost_id=created or None)
                else:
                    await client.update_cost(
                        plan.cost_id,
                        plan.campaign_id,
                        plan.year,
                        plan.month,
                        plan.conversion.daily_cost,
                    )
                return WriteOutcome(plan=plan, ok=True)
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the batch
                return WriteOutcome(plan=plan, ok=False, error=f"{type(exc).__name__}: {exc}")

    outcomes = list(await asyncio.gather(*(run(p) for p in actionable)))
    audit.record_many(tenant, outcomes)
    return outcomes
