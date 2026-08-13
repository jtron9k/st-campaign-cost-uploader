import asyncio
from decimal import Decimal

from st_cost_uploader.audit import AuditLog
from st_cost_uploader.models import Action, CostConversion, PlannedWrite
from st_cost_uploader.writer import MAX_CONCURRENCY, execute


def _plan(action=Action.CREATE, cost_id=None, campaign_id=3, month=3) -> PlannedWrite:
    conv = CostConversion(
        monthly_total=Decimal("1860.00"), days_in_month=31,
        daily_cost=Decimal("60.00"), reconstructed_total=Decimal("1860.00"),
        residual=Decimal("0.00"),
    )
    return PlannedWrite(
        campaign_id=campaign_id, campaign_name="Yelp", sheet_name="Yelp Ads",
        year=2026, month=month, action=action, conversion=conv,
        cost_id=cost_id, prior_daily_cost=None,
    )


class FakeClient:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.creates: list[tuple] = []
        self.updates: list[tuple] = []
        self.in_flight = 0
        self.peak = 0
        self._fail_on = fail_on or set()

    async def _track(self):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1

    async def create_cost(self, campaign_id, year, month, daily_cost):
        await self._track()
        if campaign_id in self._fail_on:
            raise RuntimeError("boom")
        self.creates.append((campaign_id, year, month, daily_cost))
        return 999

    async def update_cost(self, cost_id, campaign_id, year, month, daily_cost):
        await self._track()
        if campaign_id in self._fail_on:
            raise RuntimeError("boom")
        self.updates.append((cost_id, campaign_id, year, month, daily_cost))


async def test_create_calls_create(tmp_path):
    client = FakeClient()
    outcomes = await execute([_plan()], client, AuditLog(tmp_path / "w.jsonl"), "t")

    assert client.creates == [(3, 2026, 3, Decimal("60.00"))]
    assert outcomes[0].ok is True


async def test_update_calls_update(tmp_path):
    client = FakeClient()
    await execute(
        [_plan(action=Action.UPDATE, cost_id=77)],
        client, AuditLog(tmp_path / "w.jsonl"), "t",
    )

    assert client.updates == [(77, 3, 2026, 3, Decimal("60.00"))]


async def test_no_change_makes_no_call_and_is_not_reported(tmp_path):
    client = FakeClient()
    outcomes = await execute(
        [_plan(action=Action.NO_CHANGE, cost_id=77)],
        client, AuditLog(tmp_path / "w.jsonl"), "t",
    )

    assert client.creates == [] and client.updates == []
    assert outcomes == []


async def test_a_failure_does_not_stop_the_batch(tmp_path):
    client = FakeClient(fail_on={2})
    plans = [_plan(campaign_id=1), _plan(campaign_id=2), _plan(campaign_id=3)]

    outcomes = await execute(plans, client, AuditLog(tmp_path / "w.jsonl"), "t")

    assert len(outcomes) == 3
    assert sum(1 for o in outcomes if o.ok) == 2
    failed = next(o for o in outcomes if not o.ok)
    assert "boom" in failed.error


async def test_concurrency_is_capped(tmp_path):
    client = FakeClient()
    plans = [_plan(campaign_id=i) for i in range(40)]

    await execute(plans, client, AuditLog(tmp_path / "w.jsonl"), "t")

    assert client.peak <= MAX_CONCURRENCY


async def test_every_attempt_reaches_the_audit_log(tmp_path):
    path = tmp_path / "w.jsonl"
    client = FakeClient(fail_on={2})
    plans = [_plan(campaign_id=1), _plan(campaign_id=2)]

    await execute(plans, client, AuditLog(path), "acme_east")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2


async def test_outcome_order_matches_plan_order(tmp_path):
    client = FakeClient()
    plans = [_plan(campaign_id=i) for i in range(10)]

    outcomes = await execute(plans, client, AuditLog(tmp_path / "w.jsonl"), "t")

    assert [o.plan.campaign_id for o in outcomes] == list(range(10))


async def test_empty_plan_is_a_no_op(tmp_path):
    path = tmp_path / "w.jsonl"
    assert await execute([], FakeClient(), AuditLog(path), "t") == []
    assert not path.exists()
