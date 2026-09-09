import io
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from st_cost_uploader.client import ServiceTitanError
from st_cost_uploader.models import Campaign, CostRecord
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
    response = client.get("/")

    assert response.status_code == 200
    # The promise itself is static copy, so it is pinned alongside a
    # data-dependent check that the screen actually offers a way to act on
    # it: the write path only exists once a tenant is selectable.
    assert "until you approve" in response.text.lower()
    assert 'value="acme_east"' in response.text
    assert 'action="/upload"' in response.text


@pytest.fixture
def _incomplete_env(monkeypatch):
    """ST_TENANTS names a tenant whose credential block was never filled in,
    which is exactly what `cp .env.example .env` used to produce."""
    monkeypatch.setenv("ST_TENANTS", "acme_east,ghost_tenant")
    for suffix in ("ID", "CLIENT_ID", "CLIENT_SECRET", "APP_KEY"):
        monkeypatch.setenv(f"ST_TENANT_ACME_EAST_{suffix}", "x")
        monkeypatch.delenv(f"ST_TENANT_GHOST_TENANT_{suffix}", raising=False)
    return TestClient(app)


def test_index_names_the_missing_variable_when_config_is_incomplete(_incomplete_env):
    response = _incomplete_env.get("/")

    assert response.status_code == 200
    assert "ST_TENANT_GHOST_TENANT_ID" in response.text
    # No tenant survived the failure, so there is nothing to select.
    assert 'name="tenant"' not in response.text


def test_upload_names_the_missing_variable_rather_than_blaming_the_tenant(_incomplete_env):
    response = _incomplete_env.post(
        "/upload",
        # acme_east is listed in ST_TENANTS and fully credentialed, but
        # load_tenants() raises on ghost_tenant before it can return, so the
        # app sees no tenants at all. Blaming this name would be misleading.
        data={"tenant": "acme_east"},
        files={"file": ("spend.xlsx", _xlsx(), "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "ST_TENANT_GHOST_TENANT_ID" in response.text
    assert "is not a configured tenant" not in response.text


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
    # The bare word "long" also appears in the static layout-override
    # <option>, so pin the badge the parse result actually renders, plus the
    # column mapping it derived.
    assert "LONG LAYOUT" in response.text
    assert "1 rows &middot; 0 skipped" in response.text
    assert "monthly_total" in response.text


def test_upload_screen_has_one_resolve_button_and_keeps_the_choices(client):
    """The first button reads the file; only the result card resolves names.

    Before this, both buttons said "Resolve campaign names", so the operator
    had to click the same label twice to reach step 2.
    """
    before = client.get("/")
    assert before.text.count("Resolve campaign names") == 0
    assert "Check spreadsheet" in before.text

    response = client.post(
        "/upload",
        data={"tenant": "acme_east", "layout": "long"},
        files={"file": ("spend.xlsx", _xlsx(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.text.count("Resolve campaign names") == 1
    session = next(iter(SESSIONS.values()))
    assert f'action="/resolve/{session.id}"' in response.text
    # The re-upload form remembers what was submitted instead of resetting.
    assert 'value="acme_east"\n             checked' in response.text
    assert '<option value="long" selected>' in response.text


def test_pasted_rows_create_a_session_without_a_file(client):
    response = client.post(
        "/upload",
        data={"tenant": "acme_east", "pasted": "Campaign\tMonth\tSpend\nYelp\t2026-02\t1860\n"},
    )

    assert response.status_code == 200
    session = next(iter(SESSIONS.values()))
    assert session.filename == "pasted rows"
    assert session.parse_result.rows[0].campaign_name == "Yelp"
    assert "LONG LAYOUT" in response.text


def test_failed_paste_is_echoed_back_for_correction(client):
    response = client.post(
        "/upload", data={"tenant": "acme_east", "pasted": "Campaign\tNotes\nYelp\thi\n"}
    )

    assert response.status_code == 400
    assert "Campaign\tNotes" in response.text


def test_upload_with_neither_file_nor_paste_is_a_clear_400(client):
    response = client.post("/upload", data={"tenant": "acme_east", "pasted": "   "})

    assert response.status_code == 400
    assert "Choose a spreadsheet or paste rows" in response.text
    assert SESSIONS == {}


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
    # Auto-detection would have chosen long here: only one header parses as a
    # period, below the wide threshold. The override is what produced this.
    assert "WIDE LAYOUT" in response.text
    assert "period:2026-01" in response.text


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


class RaisingSTClient:
    """Stands in for a tenant whose ServiceTitan credentials don't work: every
    call that would hit the network raises the same error the real client
    raises on a non-200 token response."""

    def __init__(self, message="Could not authenticate with ServiceTitan"):
        self._message = message

    async def list_campaigns(self):
        raise ServiceTitanError(self._message)

    async def list_costs_for_campaigns(self, ids):
        raise ServiceTitanError(self._message)

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
    # "exact" alone is satisfied by the static tile label, so assert the
    # counted value beside it and the fact that nothing is left to decide.
    assert ">1</b><span>Exact name match</span>" in response.text
    assert "0 need your decision" in response.text
    assert "Nothing to decide here" in response.text


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


def _wide_same_name(client, name="Facebook Retarget") -> str:
    """A wide sheet: one campaign name spread across three months, which is
    three rows sharing a single naming decision."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Jan 2026", "Feb 2026", "Mar 2026"])
    ws.append([name, "1000", "2000", "3000"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("wide.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    return next(iter(SESSIONS))


def test_one_confirmation_resolves_every_row_with_the_same_name(client, fake_st):
    sid = _wide_same_name(client)
    client.post(f"/resolve/{sid}")

    before = SESSIONS[sid].resolutions
    assert len(before) == 3
    assert [r.is_resolved for r in before] == [False, False, False]

    client.post(f"/confirm/{sid}", data={"choice_0": "2"})

    after = SESSIONS[sid].resolutions
    assert [r.campaign_id for r in after] == [2, 2, 2]
    # All three reach the plan rather than being carried to the preview as
    # "Skipped, unmatched".
    plans = SESSIONS[sid].plans
    assert {(p.year, p.month) for p in plans} == {(2026, 1), (2026, 2), (2026, 3)}
    assert SESSIONS[sid].unresolved == []


def test_resolve_surfaces_a_service_titan_auth_failure_instead_of_500(client, monkeypatch):
    from st_cost_uploader.web import app as web

    message = "Could not authenticate with ServiceTitan for tenant 'northwind' (HTTP 400): {\"error\":\"invalid_client\"}."
    monkeypatch.setattr(web, "get_client", lambda tenant: RaisingSTClient(message))
    sid = _start(client)

    response = client.post(f"/resolve/{sid}")

    assert response.status_code == 502
    assert "invalid_client" in response.text
    # The parsed upload survives the failure, so the operator can retry
    # (after fixing .env) without re-uploading the file.
    assert f'action="/resolve/{sid}"' in response.text


def test_preview_surfaces_a_service_titan_failure_instead_of_500(client, fake_st, monkeypatch):
    from st_cost_uploader.web import app as web

    sid = _start(client)
    client.post(f"/resolve/{sid}")  # exact match on "Yelp" resolves without a confirm step

    monkeypatch.setattr(web, "get_client", lambda tenant: RaisingSTClient("network unreachable"))
    response = client.post(f"/preview/{sid}")

    assert response.status_code == 502
    assert "network unreachable" in response.text


def test_propagation_never_overrides_a_row_the_operator_decided_differently(
    client, fake_st
):
    sid = _wide_same_name(client)
    client.post(f"/resolve/{sid}")

    # The real form renders a select per pending row, so a submission can
    # carry different answers for rows that happen to share a name.
    client.post(f"/confirm/{sid}", data={"choice_0": "2", "choice_1": "1"})

    ids = [r.campaign_id for r in SESSIONS[sid].resolutions]
    # Each explicit pick stands, whichever order the form fields arrive in.
    assert ids[0] == 2
    assert ids[1] == 1
    # The unanswered row inherits a decision rather than being dropped.
    assert ids[2] is not None


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


def test_confirm_ignores_a_malformed_choice_without_dropping_the_valid_ones(
    client, fake_st
):
    # Two rows that both need a decision, submitted together: one entry is
    # corrupt, the other is a real pick. The corrupt one must be skipped
    # without aborting the rest of the submission, which a payload of
    # entirely-malformed values could never demonstrate.
    sid = _csv_upload(
        client,
        "Campaign,Month,Spend\n"
        "Facebook Retarget,2026-02,2400.00\n"
        "Yelpp,2026-02,1000.00\n",
    )
    client.post(f"/resolve/{sid}")
    assert [r.is_resolved for r in SESSIONS[sid].resolutions] == [False, False]

    response = client.post(
        f"/confirm/{sid}", data={"choice_x": "2", "choice_0": "abc", "choice_1": "1"}
    )

    assert response.status_code == 200
    assert SESSIONS[sid].resolutions[0].is_resolved is False
    assert SESSIONS[sid].resolutions[1].campaign_id == 1


def test_unknown_session_returns_404(client, fake_st):
    assert client.post("/resolve/nope").status_code == 404


def test_preview_shows_the_computed_daily_cost(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    assert response.status_code == 200
    # 5000.00 over February 2026 (28 days)
    assert "178.57" in response.text
    assert "4999.96" in response.text or "4,999.96" in response.text
    assert "-0.04" in response.text


def test_preview_marks_an_absent_record_as_create(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    # Upper-casing the whole page makes "CREATE" match the .badge.create CSS
    # rule, so match the badge as rendered instead. The PRIOR column showing
    # an em dash is what distinguishes an absent record from an update.
    assert '<span class="badge create">CREATE</span>' in response.text
    assert "<td class=\"num\">&mdash;</td>" in response.text
    assert SESSIONS[sid].plans[0].action.value == "create"


def test_preview_flags_an_overwrite_of_a_nonzero_value(client, fake_st):
    fake_st._costs = {
        (1, 2026, 2): CostRecord(id=99, campaign_id=1, year=2026, month=2,
                                 daily_cost=Decimal("71.43")),
    }
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    # "overwrite" alone is satisfied by the tr.overwrite CSS rule, so assert
    # the tinted row, the counted warning, and the prior value it exposes.
    assert '<tr class="overwrite">' in response.text
    assert "1 rows replace an existing non-zero cost" in response.text
    assert ">1</b><span>Overwrite existing value</span>" in response.text
    assert "71.43" in response.text
    assert SESSIONS[sid].plans[0].overwrites_nonzero is True


def test_preview_lists_unmatched_rows_separately(client, fake_st):
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["Yelp", "2026-02", "5000.00"])
    ws.append(["zzzz nothing like it qqqq", "2026-02", "100.00"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    sid = next(iter(SESSIONS))
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    assert len(SESSIONS[sid].plans) == 1
    assert len(SESSIONS[sid].unresolved) == 1


def test_unmatched_rows_download_as_csv(client, fake_st):
    wb = Workbook()
    ws = wb.active
    ws.append(["Campaign", "Month", "Spend"])
    ws.append(["zzzz nothing like it qqqq", "2026-02", "100.00"])
    buf = io.BytesIO()
    wb.save(buf)

    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    sid = next(iter(SESSIONS))
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    response = client.get(f"/unmatched/{sid}.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "zzzz nothing like it qqqq" in response.text


def _csv_upload(client, body: str) -> str:
    client.post(
        "/upload",
        data={"tenant": "acme_east"},
        files={"file": ("s.csv", body.encode(), "text/csv")},
    )
    return next(iter(SESSIONS))


def test_unmatched_csv_neutralises_formulas_in_campaign_names(client, fake_st):
    # The operator opens this export in Excel, where a cell starting = + - or
    # @ is evaluated rather than displayed.
    names = ["=1+1", "+1+1", "-1+1", "@SUM(1)"]
    body = "Campaign,Month,Spend\n" + "".join(
        f"{n},2026-02,100.00\n" for n in names
    )
    sid = _csv_upload(client, body)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    assert len(SESSIONS[sid].unresolved) == len(names)
    # The names survive unchanged in memory; only the export is guarded.
    assert [r.row.campaign_name for r in SESSIONS[sid].unresolved] == names

    response = client.get(f"/unmatched/{sid}.csv")
    assert response.status_code == 200

    data_lines = response.text.strip().splitlines()[1:]
    assert len(data_lines) == len(names)
    for line, name in zip(data_lines, names, strict=True):
        assert line.startswith(f"'{name},")


def test_unmatched_csv_leaves_an_ordinary_name_alone(client, fake_st):
    sid = _csv_upload(client, "Campaign,Month,Spend\nzzzz qqqq,2026-02,100.00\n")
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    response = client.get(f"/unmatched/{sid}.csv")
    assert response.text.strip().splitlines()[1].startswith("zzzz qqqq,")


def test_preview_reports_the_net_residual(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    # 5000.00 over February 2026 reconstructs as 4999.96, four cents short.
    # The label alone is static; the number is the thing the operator
    # approves.
    assert "<b>$-0.04</b><span>Net rounding residual</span>" in response.text


def test_preview_shows_a_no_change_row_as_dimmed(client, fake_st):
    # Default sheet is 5000.00 over 2026-02, which converts to 178.57/day.
    # Seeding an existing record at that exact value means the planner
    # produces NO_CHANGE, and this is the only test that ever exercises
    # that branch of the preview table.
    fake_st._costs = {
        (1, 2026, 2): CostRecord(id=99, campaign_id=1, year=2026, month=2,
                                 daily_cost=Decimal("178.57")),
    }
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    assert response.status_code == 200
    assert SESSIONS[sid].plans[0].action.value == "no_change"
    assert "NO CHANGE" in response.text.upper()


def test_write_creates_the_planned_costs(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")
    response = client.post(f"/write/{sid}")

    assert response.status_code == 200
    assert fake_st.created == [(1, 2026, 2, Decimal("178.57"))]


def test_write_updates_an_existing_record(client, fake_st):
    fake_st._costs = {
        (1, 2026, 2): CostRecord(id=99, campaign_id=1, year=2026, month=2,
                                 daily_cost=Decimal("71.43")),
    }
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")
    client.post(f"/write/{sid}")

    assert fake_st.updated == [(99, 1, 2026, 2, Decimal("178.57"))]
    assert fake_st.created == []


def test_write_reports_success_counts(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")
    response = client.post(f"/write/{sid}")

    # This screen reports what just happened to production data, so it is
    # asserted on interpolated values. The old checks were satisfied by
    # initial-scale=1 in base.html and the static "Wrote successfully" label,
    # and passed with succeeded at 0.
    assert response.status_code == 200
    assert "<h1>1 of 1 written to acme_east</h1>" in response.text
    assert ">1</b><span>Wrote successfully</span>" in response.text
    assert ">0</b><span>Failed</span>" in response.text


def test_write_records_to_the_audit_log(client, fake_st, tmp_path):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")
    client.post(f"/write/{sid}")

    lines = (tmp_path / "writes.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_a_failing_row_is_reported_without_killing_the_batch(client, fake_st):
    async def boom(*args, **kwargs):
        raise RuntimeError("ServiceTitan said no")

    fake_st.create_cost = boom

    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")
    response = client.post(f"/write/{sid}")

    assert response.status_code == 200
    assert "ServiceTitan said no" in response.text
    assert SESSIONS[sid].outcomes[0].ok is False


def test_writing_without_a_preview_is_refused(client, fake_st):
    sid = _start(client)
    assert client.post(f"/write/{sid}").status_code == 400


def _screens(client) -> dict[str, str]:
    """One rendering of each of the four wizard screens."""
    sid = _start(client)
    return {
        "upload": client.get("/").text,
        "resolve": client.post(f"/resolve/{sid}").text,
        "preview": client.post(f"/preview/{sid}").text,
        "results": client.post(f"/write/{sid}").text,
    }


def test_each_screen_highlights_its_own_step(client, fake_st):
    screens = _screens(client)
    labels = {
        "upload": "1 Upload",
        "resolve": "2 Resolve",
        "preview": "3 Preview",
        "results": "4 Results",
    }
    for screen, html in screens.items():
        highlighted = [
            label for label in labels.values()
            if f'<span class="step on">{label}</span>' in html
        ]
        assert highlighted == [labels[screen]], f"{screen} highlighted {highlighted}"


def test_no_screen_loads_a_third_party_script(client, fake_st):
    # A local tool on a production write path should render without the
    # internet, and an unused CDN script is a supply-chain surface for
    # nothing.
    for screen, html in _screens(client).items():
        assert "<script" not in html, screen
        assert "unpkg.com" not in html, screen


def test_a_resubmitted_write_is_refused_rather_than_replayed(client, fake_st, tmp_path):
    # A double-click, a browser "resend form" on reload, or a back-button
    # resubmit all re-POST the same URL. The plan still says CREATE for rows
    # created seconds earlier, so replaying it would duplicate production
    # writes.
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    first = client.post(f"/write/{sid}")
    second = client.post(f"/write/{sid}")

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already ran" in second.text.lower()
    assert fake_st.created == [(1, 2026, 2, Decimal("178.57"))]

    audit_lines = (tmp_path / "writes.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["new_daily_cost"] == "178.57"


def test_a_resubmitted_no_change_batch_is_also_refused(client, fake_st):
    # A batch of only NO_CHANGE rows attempts nothing, so outcomes stays
    # empty and cannot be the guard on its own. Clearing the plan is what
    # closes this path.
    fake_st._costs = {
        (1, 2026, 2): CostRecord(id=99, campaign_id=1, year=2026, month=2,
                                 daily_cost=Decimal("178.57")),
    }
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/preview/{sid}")

    assert client.post(f"/write/{sid}").status_code == 200
    assert SESSIONS[sid].outcomes == []
    assert SESSIONS[sid].plans == []
    assert client.post(f"/write/{sid}").status_code == 400
    assert fake_st.created == []
    assert fake_st.updated == []


def test_the_long_sample_downloads_as_an_attachment(client):
    response = client.get("/sample/long.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "campaign-costs-sample-long.csv" in response.headers["content-disposition"]
    assert response.text.startswith("Campaign,Month,Total Spend")


def test_the_wide_sample_downloads_as_an_attachment(client):
    response = client.get("/sample/wide.csv")

    assert response.status_code == 200
    assert "campaign-costs-sample-wide.csv" in response.headers["content-disposition"]
    assert response.text.startswith("Campaign,")


def test_an_unknown_sample_layout_is_a_404(client):
    assert client.get("/sample/sideways.csv").status_code == 404


def test_the_sample_route_needs_no_tenant_configured(monkeypatch):
    """It is reachable before the operator has chosen anything, so a broken
    or absent tenant config must not take it down with the upload screen."""
    monkeypatch.delenv("ST_TENANTS", raising=False)

    assert TestClient(app).get("/sample/long.csv").status_code == 200


def test_the_upload_screen_links_both_samples(client):
    body = client.get("/").text

    assert "/sample/long.csv" in body
    assert "/sample/wide.csv" in body
