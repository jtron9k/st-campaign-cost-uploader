import io
from decimal import Decimal  # noqa: F401 -- kept per the brief, unused in these tests

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from st_cost_uploader.models import Campaign, CostRecord  # noqa: F401 -- CostRecord unused here
from st_cost_uploader.web.app import SESSIONS, app


@pytest.fixture(autouse=True)
def _clear_sessions():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ST_TENANTS", "acme_east,northwind")
    for slug in ("ACME_EAST", "NORTHWIND"):
        monkeypatch.setenv(f"ST_TENANT_{slug}_ID", "1")
        monkeypatch.setenv(f"ST_TENANT_{slug}_CLIENT_ID", "cid")
        monkeypatch.setenv(f"ST_TENANT_{slug}_CLIENT_SECRET", "sec")
        monkeypatch.setenv(f"ST_TENANT_{slug}_APP_KEY", "ak")
    return TestClient(app)


def _xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["Yelp", "2026-02", "5000.00"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_index_lists_configured_tenants(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "acme_east" in response.text
    assert "northwind" in response.text


def test_index_says_nothing_is_written_yet(client):
    assert "until you approve" in client.get("/").text.lower()


def test_upload_creates_a_session_and_reports_detection(client):
    response = client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("spend.xlsx", _xlsx(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 200
    assert len(SESSIONS) == 1
    session = next(iter(SESSIONS.values()))
    assert session.tenant == "acme_east"
    assert session.parse_result.layout == "long"
    assert "long" in response.text.lower()


def test_upload_rejects_an_unknown_tenant(client):
    response = client.post(
        "/upload",
        data={"tenant": "not_a_tenant"},
        files={"file": ("spend.xlsx", _xlsx(), "application/octet-stream")},
    )

    assert response.status_code == 400
    assert SESSIONS == {}


def test_upload_rejects_an_unreadable_file(client):
    response = client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("junk.xlsx", b"not a spreadsheet", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert SESSIONS == {}


def test_layout_override_is_honoured(client):
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Jan 2026", "Notes"])
    ws.append(["Yelp", "1000", "ignore"])
    buf = io.BytesIO()
    wb.save(buf)

    response = client.post(
        "/upload",
        data={"tenant": "acme_east", "layout": "wide"},
        files={"file": ("spend.xlsx", buf.getvalue(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert next(iter(SESSIONS.values())).parse_result.layout == "wide"


class FakeSTClient:
    def __init__(self, campaigns=None, costs=None):
        self._campaigns = campaigns or [
            Campaign(id=1, name="Yelp", active=True),
            Campaign(id=2, name="Facebook Retargeting", active=True),
        ]
        self._costs = costs or {}
        self.created: list[tuple] = []
        self.updated: list[tuple] = []

    async def list_campaigns(self):
        return self._campaigns

    async def list_costs_for_campaigns(self, ids):
        return {k: v for k, v in self._costs.items() if k[0] in set(ids)}

    async def create_cost(self, campaign_id, year, month, daily_cost):
        self.created.append((campaign_id, year, month, daily_cost))
        return 1

    async def update_cost(self, cost_id, campaign_id, year, month, daily_cost):
        self.updated.append((cost_id, campaign_id, year, month, daily_cost))

    async def aclose(self):
        pass


@pytest.fixture
def fake_st(monkeypatch, tmp_path):
    from st_cost_uploader.web import app as web

    stub = FakeSTClient()
    monkeypatch.setattr(web, "get_client", lambda tenant: stub)
    monkeypatch.setattr(web, "ALIAS_DIR", tmp_path / "aliases")
    monkeypatch.setattr(web, "AUDIT_PATH", tmp_path / "writes.jsonl")
    return stub


def _start(client) -> str:
    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("spend.xlsx", _xlsx(), "application/octet-stream")},
    )
    return next(iter(SESSIONS))


def test_resolve_shows_an_exact_match_as_resolved(client, fake_st):
    sid = _start(client)
    response = client.post(f"/resolve/{sid}")

    assert response.status_code == 200
    session = SESSIONS[sid]
    assert session.resolutions[0].campaign_id == 1
    assert "exact" in response.text.lower()


def test_resolve_offers_candidates_for_a_near_match(client, fake_st):
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["Facebook Retarget", "2026-02", "2400.00"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    sid = next(iter(SESSIONS))
    response = client.post(f"/resolve/{sid}")

    assert response.status_code == 200
    assert "Facebook Retargeting" in response.text
    assert SESSIONS[sid].resolutions[0].is_resolved is False


def test_confirming_a_choice_saves_an_alias(client, fake_st, tmp_path):
    import json

    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["Facebook Retarget", "2026-02", "2400.00"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    sid = next(iter(SESSIONS))
    client.post(f"/resolve/{sid}")
    client.post(f"/confirm/{sid}", data={"choice_0": "2"})

    saved = json.loads((tmp_path / "aliases" / "acme_east.json").read_text())
    assert saved == {"facebook retarget": 2}
    assert SESSIONS[sid].resolutions[0].campaign_id == 2


def test_skipping_a_row_leaves_it_unresolved(client, fake_st):
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["Facebook Retarget", "2026-02", "2400.00"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    sid = next(iter(SESSIONS))
    client.post(f"/resolve/{sid}")

    before = SESSIONS[sid].resolutions[0]
    assert before.kind.value == "fuzzy"
    assert before.is_resolved is False
    assert before.candidates

    client.post(f"/confirm/{sid}", data={"choice_0": "skip"})

    after = SESSIONS[sid].resolutions[0]
    assert after.is_resolved is False
    assert after.candidates == before.candidates


def test_confirm_ignores_a_malformed_choice_without_500(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")

    response = client.post(
        f"/confirm/{sid}", data={"choice_x": "2", "choice_0": "abc"}
    )

    assert response.status_code == 200
    assert SESSIONS[sid].resolutions[0].campaign_id == 1


def test_unknown_session_returns_404(client, fake_st):
    assert client.post("/resolve/nope").status_code == 404
