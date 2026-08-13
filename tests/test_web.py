import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

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
