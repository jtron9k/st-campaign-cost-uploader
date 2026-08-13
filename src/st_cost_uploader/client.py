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
                f"'{self._config.name}' (HTTP {response.status_code}). "
                "Check the client ID, secret, and tenant ID in .env."
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
        return CostRecord(
            id=r["id"],
            campaign_id=r["campaignId"],
            year=r["year"],
            month=r["month"],
            daily_cost=_to_decimal(r.get("dailyCost")),
        )

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
                record = CostRecord(
                    id=r["id"],
                    campaign_id=r["campaignId"],
                    year=r["year"],
                    month=r["month"],
                    daily_cost=_to_decimal(r.get("dailyCost")),
                )
                found[record.period_key] = record
        return found
