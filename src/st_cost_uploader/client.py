"""Async ServiceTitan Marketing v2 client.

Endpoints and header names are copied from the verified reference table in
the implementation plan. All money crossing this boundary becomes Decimal
immediately, via str(), so no float ever reaches the money path.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Self

import httpx

from st_cost_uploader.config import TenantConfig
from st_cost_uploader.models import Campaign, CostRecord, PeriodKey

TOKEN_URL = "https://auth.servicetitan.io/connect/token"
API_BASE = "https://api.servicetitan.io"
TOKEN_LIFETIME = 900
TOKEN_BUFFER = 60
PAGE_SIZE = 500
REQUEST_TIMEOUT = 30.0


class ServiceTitanError(Exception):
    """Any non-success response from ServiceTitan."""


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _cost_record(row: dict) -> CostRecord:
    return CostRecord(
        id=row["id"],
        campaign_id=row["campaignId"],
        year=row["year"],
        month=row["month"],
        daily_cost=_to_decimal(row.get("dailyCost")),
    )


class ServiceTitanClient:
    def __init__(
        self,
        config: TenantConfig,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._http = httpx.AsyncClient(transport=transport, timeout=REQUEST_TIMEOUT)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- auth -----------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and self._token_expires_at > time.monotonic():
            return self._token

        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "tenant": self._config.tenant_id,
            },
        )
        if response.status_code != 200:
            raise ServiceTitanError(
                f"Could not authenticate with ServiceTitan for tenant "
                f"'{self._config.name}' (HTTP {response.status_code}): "
                f"{response.text}. Check the client ID, secret, and tenant ID in .env."
            )

        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + TOKEN_LIFETIME - TOKEN_BUFFER
        return self._token

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._access_token()}",
            "ST-App-Key": self._config.app_key,
            "Content-Type": "application/json",
        }

    # ---- transport ------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        url = f"{API_BASE}{path}"
        response = await self._http.request(
            method, url, headers=await self._headers(), params=params, json=json_body
        )
        if response.status_code >= 400:
            raise ServiceTitanError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return {}
        return response.json()

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Walk pages until hasMore is false. Trusts hasMore, not item count."""
        items: list[dict] = []
        page = 1
        while True:
            payload = await self._request(
                "GET", path, params={**(params or {}), "page": page, "pageSize": PAGE_SIZE}
            )
            items.extend(payload.get("data", []))
            if not payload.get("hasMore"):
                return items
            page += 1

    def _tenant_path(self, suffix: str) -> str:
        return f"/marketing/v2/tenant/{self._config.tenant_id}/{suffix}"

    # ---- reads ----------------------------------------------------------

    async def list_campaigns(self) -> list[Campaign]:
        rows = await self._paginate(self._tenant_path("campaigns"))
        return [
            Campaign(id=r["id"], name=r.get("name") or "", active=bool(r.get("active")))
            for r in rows
        ]

    async def get_cost(self, cost_id: int) -> CostRecord:
        r = await self._request("GET", self._tenant_path(f"costs/{cost_id}"))
        return _cost_record(r)

    async def list_costs_for_campaigns(
        self, campaign_ids: list[int]
    ) -> dict[PeriodKey, CostRecord]:
        """Fetch existing costs for the given campaigns, keyed by period.

        Filtering per campaign keeps the read cost proportional to the sheet
        rather than to the tenant's entire cost history.
        """
        found: dict[PeriodKey, CostRecord] = {}
        for campaign_id in dict.fromkeys(campaign_ids):
            rows = await self._paginate(
                self._tenant_path("costs"), {"campaignId": campaign_id}
            )
            for r in rows:
                record = _cost_record(r)
                found[record.period_key] = record
        return found

    # ---- writes -----------------------------------------------------------

    @staticmethod
    def _cost_body(campaign_id: int, year: int, month: int, daily_cost: Decimal) -> dict:
        # float() here is the single, deliberate crossing point into JSON.
        # The value is already quantized to 2dp, which float represents
        # exactly enough for transport; it is never used for arithmetic.
        return {
            "campaignId": campaign_id,
            "year": year,
            "month": month,
            "dailyCost": float(daily_cost),
        }

    async def create_cost(
        self, campaign_id: int, year: int, month: int, daily_cost: Decimal
    ) -> int:
        payload = await self._request(
            "POST",
            self._tenant_path("costs"),
            json_body=self._cost_body(campaign_id, year, month, daily_cost),
        )
        # Verified against production 2026-08-13 (one live tenant,
        # a campaign with no prior records, 2026-11): POST's response carries NO id. The
        # record was genuinely created -- a follow-up read returned it as
        # with a real id -- but nothing in the response body names it, so this
        # returns 0. Contrast PATCH, whose response is {"id": ...} (verified
        # 2026-08-12 on a live record).
        #
        # A 0 therefore means "no id in the response", which is now the
        # expected case, and never "the write failed" -- a real failure
        # raises ServiceTitanError before this line runs.
        #
        # Do NOT build a follow-up request path (e.g. PATCH .../costs/{id})
        # out of this value. To learn a created record's id, re-read the
        # campaign's costs and match on (year, month).
        return int(payload.get("id", 0))

    async def update_cost(
        self, cost_id: int, campaign_id: int, year: int, month: int, daily_cost: Decimal
    ) -> None:
        # Verified against production (2026-08-12, one live tenant,
        # one live record, supervised write + restore): PATCH accepts and
        # requires only {"dailyCost": ...}. campaign_id/year/month identify
        # which record this is, and are accepted here only to keep the
        # signature stable for callers (Task 12) -- do NOT add them back
        # into the body. Sending identifying fields on an update to an
        # existing record was never verified and risks re-keying the row
        # to the wrong campaign-month.
        del campaign_id, year, month
        await self._request(
            "PATCH",
            self._tenant_path(f"costs/{cost_id}"),
            json_body={"dailyCost": float(daily_cost)},
        )
