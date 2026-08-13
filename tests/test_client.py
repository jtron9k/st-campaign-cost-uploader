import json
from decimal import Decimal

import httpx
import pytest

from st_cost_uploader.client import API_BASE, ServiceTitanClient, ServiceTitanError
from st_cost_uploader.config import TenantConfig

CONFIG = TenantConfig(
    name="t", tenant_id="999", client_id="cid", client_secret="sec", app_key="ak"
)


def _client(handler) -> ServiceTitanClient:
    transport = httpx.MockTransport(handler)
    return ServiceTitanClient(CONFIG, transport=transport)


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 900})


async def test_token_is_requested_once_and_reused():
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            calls["token"] += 1
            return _token_response()
        return httpx.Response(200, json={"hasMore": False, "data": []})

    client = _client(handler)
    await client.list_campaigns()
    await client.list_campaigns()

    assert calls["token"] == 1


async def test_auth_headers_are_sent():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen.update(request.headers)
        return httpx.Response(200, json={"hasMore": False, "data": []})

    await _client(handler).list_campaigns()

    assert seen["authorization"] == "Bearer tok-123"
    assert seen["st-app-key"] == "ak"


async def test_token_request_sends_the_tenant_id():
    body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            body["content"] = request.content.decode()
            return _token_response()
        return httpx.Response(200, json={"hasMore": False, "data": []})

    await _client(handler).list_campaigns()

    assert "grant_type=client_credentials" in body["content"]
    assert "tenant=999" in body["content"]


async def test_list_campaigns_follows_pagination_using_has_more():
    pages = {
        1: {"hasMore": True, "data": [{"id": 1, "name": "A", "active": True}]},
        2: {"hasMore": False, "data": [{"id": 2, "name": "B", "active": False}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages[page])

    campaigns = await _client(handler).list_campaigns()

    assert [c.id for c in campaigns] == [1, 2]
    assert campaigns[1].active is False


async def test_list_campaigns_hits_the_right_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["path"] = request.url.path
        return httpx.Response(200, json={"hasMore": False, "data": []})

    await _client(handler).list_campaigns()

    assert seen["path"] == "/marketing/v2/tenant/999/campaigns"


async def test_costs_are_fetched_per_campaign_and_keyed_by_period():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        cid = int(request.url.params["campaignId"])
        return httpx.Response(200, json={
            "hasMore": False,
            "data": [
                {"id": cid * 10, "year": 2026, "month": 2,
                 "dailyCost": 12.34, "campaignId": cid},
            ],
        })

    costs = await _client(handler).list_costs_for_campaigns([7, 8])

    assert set(costs) == {(7, 2026, 2), (8, 2026, 2)}
    assert costs[(7, 2026, 2)].id == 70
    assert costs[(7, 2026, 2)].daily_cost == Decimal("12.34")


async def test_daily_cost_is_decimal_not_float():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        return httpx.Response(200, json={"hasMore": False, "data": [
            {"id": 1, "year": 2026, "month": 2, "dailyCost": 178.57, "campaignId": 5},
        ]})

    costs = await _client(handler).list_costs_for_campaigns([5])
    value = costs[(5, 2026, 2)].daily_cost

    assert isinstance(value, Decimal)
    assert value == Decimal("178.57")


async def test_duplicate_campaign_ids_are_fetched_once():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        calls["n"] += 1
        return httpx.Response(200, json={"hasMore": False, "data": []})

    await _client(handler).list_costs_for_campaigns([5, 5, 5])

    assert calls["n"] == 1


async def test_get_cost_returns_the_single_record():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "id": 500000001, "year": 2022, "month": 1,
            "dailyCost": 0.0, "campaignId": 1000001,
        })

    record = await _client(handler).get_cost(500000001)

    assert seen["path"] == "/marketing/v2/tenant/999/costs/500000001"
    assert record.id == 500000001
    assert record.campaign_id == 1000001
    assert record.year == 2022
    assert record.month == 1
    assert isinstance(record.daily_cost, Decimal)
    assert record.daily_cost == Decimal("0.0")


async def test_get_cost_daily_cost_is_decimal_and_reflects_a_nonzero_value():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        return httpx.Response(200, json={
            "id": 1, "year": 2026, "month": 2,
            "dailyCost": 178.57, "campaignId": 5,
        })

    record = await _client(handler).get_cost(1)

    assert isinstance(record.daily_cost, Decimal)
    assert record.daily_cost == Decimal("178.57")


async def test_http_error_becomes_service_titan_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(ServiceTitanError, match="403"):
        await _client(handler).list_campaigns()


async def test_bad_credentials_surface_immediately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(ServiceTitanError, match="authenticate"):
        await _client(handler).list_campaigns()


async def test_api_base_is_the_documented_host():
    assert API_BASE == "https://api.servicetitan.io"


async def test_create_cost_posts_the_documented_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id": 555})

    new_id = await _client(handler).create_cost(7, 2026, 2, Decimal("178.57"))

    assert new_id == 555
    assert seen["method"] == "POST"
    assert seen["path"] == "/marketing/v2/tenant/999/costs"
    # httpx serializes JSON with compact separators (no space after ":"),
    # so these assertions match the real wire format, not textbook json.dumps.
    assert '"campaignId":7' in seen["body"]
    assert '"year":2026' in seen["body"]
    assert '"month":2' in seen["body"]
    assert "178.57" in seen["body"]


async def test_daily_cost_is_serialized_as_a_number_not_a_string():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id": 1})

    await _client(handler).create_cost(7, 2026, 2, Decimal("178.57"))

    assert '"dailyCost":178.57' in seen["body"]
    assert '"dailyCost":"178.57"' not in seen["body"]


async def test_update_cost_targets_the_record_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={})

    await _client(handler).update_cost(555, 7, 2026, 2, Decimal("85.71"))

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/marketing/v2/tenant/999/costs/555"
    assert "85.71" in seen["body"]


async def test_update_cost_sends_only_the_daily_cost():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id": 555})

    await _client(handler).update_cost(555, 7, 2026, 2, Decimal("85.71"))

    body = json.loads(seen["body"])
    assert body == {"dailyCost": 85.71}


async def test_write_failure_raises_with_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        return httpx.Response(422, text="validation failed")

    with pytest.raises(ServiceTitanError, match="422"):
        await _client(handler).create_cost(7, 2026, 2, Decimal("1.00"))
