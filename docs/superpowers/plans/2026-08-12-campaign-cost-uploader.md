# ST Campaign Cost Uploader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web app that reads a spreadsheet of monthly campaign spend and writes it to ServiceTitan's Marketing v2 cost API, handling monthly-to-daily conversion, campaign name resolution, and create-versus-update.

**Architecture:** A pure Python engine package (`st_cost_uploader`) with no web dependency, wrapped by a thin FastAPI layer that server-renders Jinja2 templates with htmx. Data moves through a fixed pipeline: parse to canonical rows, resolve names to campaign IDs, convert money, plan create/update/no-change, present a preview gate, then write. Nothing reaches ServiceTitan before the operator approves the preview.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Jinja2, htmx, httpx (async), openpyxl, rapidfuzz, pytest, pytest-asyncio, ruff, uv.

## Global Constraints

- **Money is `Decimal` end to end.** No `float` anywhere in the money path. Parse via `Decimal(str(value))`, never `Decimal(float_value)`.
- **Rounding is `ROUND_HALF_UP` to exactly 2 decimal places.** ServiceTitan stores `dailyCost` to the cent (verified 2026-08-12, 500 records, no value above 2dp).
- **`days_in_month` comes from `calendar.monthrange`.** Never a hardcoded table.
- **A cost record is unique per `(campaignId, year, month)`.** Always look up before writing.
- **Never commit credentials.** All ServiceTitan secrets live in a gitignored `.env`.
- **Writes go only to the tenant selected in the UI**, and only after the preview gate is approved.
- **Max 8 concurrent write requests.** ServiceTitan allows roughly 60/sec; the existing MCP client self-limits near 30.
- **Fuzzy match floor is score 60, max 5 candidates offered.** An alias always beats a higher-scoring fuzzy candidate.
- **Paging trusts `hasMore`, not the returned item count.**

### Verified API reference (use these exact values)

| Item | Value |
| --- | --- |
| Token URL | `https://auth.servicetitan.io/connect/token` |
| Token request | POST form: `grant_type=client_credentials`, `client_id`, `client_secret`, `tenant=<tenant_id>` |
| Token lifetime | 900s; refresh 60s early |
| API base | `https://api.servicetitan.io` |
| Headers | `Authorization: Bearer <token>`, `ST-App-Key: <app_key>`, `Content-Type: application/json` |
| List campaigns | `GET /marketing/v2/tenant/{tenant_id}/campaigns?page=&pageSize=` |
| List costs | `GET /marketing/v2/tenant/{tenant_id}/costs?campaignId=&page=&pageSize=` (filter confirmed working 2026-08-12) |
| Get one cost | `GET /marketing/v2/tenant/{tenant_id}/costs/{id}` (confirmed 2026-08-12) |
| Create cost | `POST /marketing/v2/tenant/{tenant_id}/costs` body `{campaignId, year, month, dailyCost}` |
| Update cost | **UNCONFIRMED. Task 1 resolves this.** |
| List envelope | `{"page":1,"pageSize":50,"hasMore":true,"totalCount":null,"data":[...]}` |
| Cost record | `{"id":500000001,"year":2022,"month":1,"dailyCost":0.0,"campaignId":1000001}` |

---

## File Structure

```
pyproject.toml                          Project metadata, deps, pytest/ruff config
.env.example                            Credential template, committed
src/st_cost_uploader/
  __init__.py
  models.py         Task 2   Frozen dataclasses and enums. No behavior.
  normalize.py      Task 5   normalize_name(). Shared by aliases and resolver.
  money.py          Task 3   days_in_month(), convert(). The Decimal path.
  parser.py         Task 4   Spreadsheet reading, layout detection, canonical rows.
  aliases.py        Task 5   Per-tenant JSON alias store.
  resolver.py       Task 6   exact -> alias -> fuzzy name resolution.
  config.py         Task 7   Env loading into TenantConfig.
  client.py         Task 8/9 ServiceTitan HTTP client. Auth, reads, writes.
  planner.py        Task 10  CREATE / UPDATE / NO_CHANGE decision.
  audit.py          Task 11  Append-only JSONL write log.
  writer.py         Task 12  Concurrent plan execution.
  web/
    __init__.py
    app.py          Task 13  FastAPI app, session store, routes.
    templates/
      base.html     Task 13  Shell, CSS tokens, top bar, step indicator.
      upload.html   Task 13  Screen 1.
      resolve.html  Task 14  Screen 2.
      preview.html  Task 15  Screen 3.
      results.html  Task 16  Screen 4.
tests/
  conftest.py       Task 2   Shared fixtures.
  test_money.py     Task 3
  test_parser.py    Task 4
  test_aliases.py   Task 5
  test_resolver.py  Task 6
  test_config.py    Task 7
  test_client.py    Task 8/9
  test_planner.py   Task 10
  test_audit.py     Task 11
  test_writer.py    Task 12
  test_web.py       Task 13-16
  fixtures/         Task 4   Generated spreadsheets, anonymized.
aliases/                     Tracked in git. Per-tenant name maps.
logs/                        Gitignored. Audit JSONL.
```

---

### Task 1: Confirm the cost update verb against the live API

**This task writes to a production ServiceTitan tenant. Stop and get the operator's explicit approval before executing step 3.** Do not proceed without it.

This is the one unknown in the spec. Everything in Task 9 depends on the answer, so it resolves first.

**Files:**
- Modify: `CLAUDE.md` (the "Writes are an upsert, not a create" section)

**Interfaces:**
- Consumes: nothing
- Produces: the confirmed verb and path, recorded in `CLAUDE.md`, consumed by Task 9

- [ ] **Step 1: Pick a target that is currently zero**

Use the `servicetitan-local` MCP server, one live tenant. Campaign `1000001` has records back to 2022-01, all at `dailyCost: 0.0`.

Read the specific record to confirm its current state:

```
mcp__servicetitan-local__servicetitan_api_call
  tenant: acme_east
  method: GET
  path: /marketing/v2/tenant/{tenant_id}/costs/500000001
```

Expected: `{"id": 500000001, "year": 2022, "month": 1, "dailyCost": 0.0, "campaignId": 1000001}`

Record the exact response. If `dailyCost` is not `0.0`, stop and pick a different record that is, because this task restores the prior value and a non-zero starting point means someone is using it.

- [ ] **Step 2: Get the operator's approval**

Show him the target record and the exact request you are about to send. Wait for a clear yes. This is a production tenant with real business data.

- [ ] **Step 3: Attempt PATCH**

```
mcp__servicetitan-local__servicetitan_api_call
  tenant: acme_east
  method: PATCH
  path: /marketing/v2/tenant/{tenant_id}/costs/500000001
  body: {"dailyCost": 1.23}
```

Record the status and response body verbatim.

If PATCH returns 404 or 405, retry with `PUT` and the full body `{"campaignId": 1000001, "year": 2022, "month": 1, "dailyCost": 1.23}`. If both fail, record both failures and stop; the write path needs a different design and that is a decision for the operator, not a workaround.

- [ ] **Step 4: Re-read to confirm the write landed**

```
mcp__servicetitan-local__servicetitan_api_call
  tenant: acme_east
  method: GET
  path: /marketing/v2/tenant/{tenant_id}/costs/500000001
```

Expected: `dailyCost` is now `1.23`.

Note whether the returned `id` is unchanged. If a new `id` came back, the operation created a row rather than updating one, which changes the design.

- [ ] **Step 5: Restore the original value**

Send the same verb that worked, with `{"dailyCost": 0.0}`. Then GET once more and confirm it reads `0.0` again. Do not skip this. Leaving test data in a production tenant is not acceptable.

- [ ] **Step 6: Record the finding in CLAUDE.md**

Replace the paragraph beginning "`GET /marketing/v2/tenant/{tenant_id}/costs/{id}` returns a single record" with the confirmed verb, the exact request body shape that worked, the response status, and the date. Follow the existing convention in that file: state what was verified, against which tenant, on what date.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: confirm the cost update verb against live API"
```

---

### Task 2: Project scaffolding and domain models

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/st_cost_uploader/__init__.py`
- Create: `src/st_cost_uploader/models.py`
- Create: `tests/conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing
- Produces: every type used by every later task. `SheetRow`, `ParseError`, `ParseResult`, `Campaign`, `CostRecord`, `MatchKind`, `Candidate`, `Resolution`, `CostConversion`, `Action`, `PlannedWrite`, `WriteOutcome`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "st-cost-uploader"
version = "0.1.0"
description = "Bulk-upload marketing campaign costs into ServiceTitan from a spreadsheet"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "jinja2>=3.1",
    "python-multipart>=0.0.12",
    "httpx>=0.27",
    "openpyxl>=3.1",
    "rapidfuzz>=3.10",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "ruff>=0.7"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/st_cost_uploader"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
src = ["src", "tests"]
```

- [ ] **Step 2: Install and confirm the toolchain works**

```bash
uv sync --extra dev
```

Then:

```bash
uv run pytest --version
```

Expected: a pytest version prints with no import errors.

- [ ] **Step 3: Create .env.example**

```bash
# ServiceTitan credentials. Copy to .env and fill in. Never commit .env.
# Comma-separated list of tenant slugs this install can write to.
ST_TENANTS=acme_east,acme_west,northwind,globex

# One block per tenant. The slug is uppercased and embedded in the var name.
ST_TENANT_ACME_EAST_ID=
ST_TENANT_ACME_EAST_CLIENT_ID=
ST_TENANT_ACME_EAST_CLIENT_SECRET=
ST_TENANT_ACME_EAST_APP_KEY=
```

- [ ] **Step 4: Write the failing test**

`tests/test_models.py`:

```python
from decimal import Decimal

from st_cost_uploader.models import (
    Action,
    Campaign,
    CostConversion,
    CostRecord,
    MatchKind,
    PlannedWrite,
    Resolution,
    SheetRow,
)


def test_sheet_row_is_frozen_and_hashable():
    row = SheetRow(
        campaign_name="Yelp", year=2026, month=3,
        monthly_total=Decimal("1860.00"), source_row=2,
    )
    assert {row}
    assert row.monthly_total == Decimal("1860.00")


def test_cost_record_period_key():
    rec = CostRecord(id=1, campaign_id=99, year=2026, month=3, daily_cost=Decimal("60.00"))
    assert rec.period_key == (99, 2026, 3)


def test_planned_write_period_key_matches_cost_record():
    conv = CostConversion(
        monthly_total=Decimal("1860.00"), days_in_month=31,
        daily_cost=Decimal("60.00"), reconstructed_total=Decimal("1860.00"),
        residual=Decimal("0.00"),
    )
    plan = PlannedWrite(
        campaign_id=99, campaign_name="Yelp", sheet_name="Yelp Ads",
        year=2026, month=3, action=Action.CREATE, conversion=conv,
        cost_id=None, prior_daily_cost=None,
    )
    assert plan.period_key == (99, 2026, 3)


def test_resolution_defaults_to_no_candidates():
    row = SheetRow("Yelp", 2026, 3, Decimal("10"), 2)
    res = Resolution(row=row, kind=MatchKind.NONE, campaign_id=None, campaign_name=None)
    assert res.candidates == ()
    assert res.is_resolved is False


def test_resolution_is_resolved_when_campaign_id_present():
    row = SheetRow("Yelp", 2026, 3, Decimal("10"), 2)
    res = Resolution(row=row, kind=MatchKind.EXACT, campaign_id=99, campaign_name="Yelp")
    assert res.is_resolved is True


def test_campaign_construction():
    c = Campaign(id=1, name="Search||Google||Brand", active=True)
    assert c.name.endswith("Brand")
```

- [ ] **Step 5: Run the test to verify it fails**

```bash
uv run pytest tests/test_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.models'`

- [ ] **Step 6: Write models.py**

`src/st_cost_uploader/models.py`:

```python
"""Domain types. Frozen, no behavior beyond derived properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

PeriodKey = tuple[int, int, int]


@dataclass(frozen=True)
class SheetRow:
    """One canonical row, whatever layout it came from."""

    campaign_name: str
    year: int
    month: int
    monthly_total: Decimal
    source_row: int


@dataclass(frozen=True)
class ParseError:
    source_row: int
    message: str


@dataclass(frozen=True)
class ParseResult:
    layout: str
    rows: tuple[SheetRow, ...]
    errors: tuple[ParseError, ...]
    column_mapping: dict[str, str]


@dataclass(frozen=True)
class Campaign:
    id: int
    name: str
    active: bool


@dataclass(frozen=True)
class CostRecord:
    id: int
    campaign_id: int
    year: int
    month: int
    daily_cost: Decimal

    @property
    def period_key(self) -> PeriodKey:
        return (self.campaign_id, self.year, self.month)


class MatchKind(str, Enum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass(frozen=True)
class Candidate:
    campaign_id: int
    campaign_name: str
    score: int


@dataclass(frozen=True)
class Resolution:
    row: SheetRow
    kind: MatchKind
    campaign_id: int | None
    campaign_name: str | None
    candidates: tuple[Candidate, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.campaign_id is not None


@dataclass(frozen=True)
class CostConversion:
    monthly_total: Decimal
    days_in_month: int
    daily_cost: Decimal
    reconstructed_total: Decimal
    residual: Decimal


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    NO_CHANGE = "no_change"


@dataclass(frozen=True)
class PlannedWrite:
    campaign_id: int
    campaign_name: str
    sheet_name: str
    year: int
    month: int
    action: Action
    conversion: CostConversion
    cost_id: int | None
    prior_daily_cost: Decimal | None

    @property
    def period_key(self) -> PeriodKey:
        return (self.campaign_id, self.year, self.month)

    @property
    def overwrites_nonzero(self) -> bool:
        return (
            self.action is Action.UPDATE
            and self.prior_daily_cost is not None
            and self.prior_daily_cost != Decimal("0")
        )


@dataclass(frozen=True)
class WriteOutcome:
    plan: PlannedWrite
    ok: bool
    error: str | None = None
```

Create `src/st_cost_uploader/__init__.py` as an empty file, and `tests/conftest.py` as an empty file for now.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/test_models.py -v
```

Expected: 6 passed

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .env.example src/st_cost_uploader/__init__.py src/st_cost_uploader/models.py tests/conftest.py tests/test_models.py uv.lock
git commit -m "feat: scaffold project and add domain models"
```

---

### Task 3: The money module

This is where being wrong costs real money. The tests come from the spec's worked examples.

**Files:**
- Create: `src/st_cost_uploader/money.py`
- Test: `tests/test_money.py`

**Interfaces:**
- Consumes: `CostConversion` from Task 2
- Produces: `days_in_month(year, month) -> int`, `convert(monthly_total: Decimal, year: int, month: int) -> CostConversion`, `parse_money(value) -> Decimal | None`

- [ ] **Step 1: Write the failing test**

`tests/test_money.py`:

```python
from decimal import Decimal

import pytest

from st_cost_uploader.money import convert, days_in_month, parse_money


@pytest.mark.parametrize(
    "year,month,expected",
    [
        (2024, 2, 29),  # leap
        (2026, 2, 28),  # not leap
        (2000, 2, 29),  # divisible by 400
        (1900, 2, 28),  # divisible by 100 but not 400
        (2026, 1, 31),
        (2026, 4, 30),
    ],
)
def test_days_in_month(year, month, expected):
    assert days_in_month(year, month) == expected


def test_five_thousand_over_february_2026_loses_four_cents():
    c = convert(Decimal("5000.00"), 2026, 2)
    assert c.days_in_month == 28
    assert c.daily_cost == Decimal("178.57")
    assert c.reconstructed_total == Decimal("4999.96")
    assert c.residual == Decimal("-0.04")


def test_seven_thousand_over_leap_february_gains_two_cents():
    c = convert(Decimal("7000.00"), 2024, 2)
    assert c.days_in_month == 29
    assert c.daily_cost == Decimal("241.38")
    assert c.reconstructed_total == Decimal("7000.02")
    assert c.residual == Decimal("0.02")


def test_exact_division_leaves_no_residual():
    c = convert(Decimal("1860.00"), 2026, 3)
    assert c.daily_cost == Decimal("60.00")
    assert c.residual == Decimal("0.00")


def test_rounding_is_half_up_not_bankers():
    # Both cases land exactly on a half-cent, where the two rounding modes
    # disagree. This is the guard against forgetting the rounding argument,
    # since Python's Decimal default is ROUND_HALF_EVEN.
    #
    # 0.775 / 31 = 0.025 exactly -> half-up 0.03, half-even 0.02
    assert convert(Decimal("0.775"), 2026, 1).daily_cost == Decimal("0.03")
    # 1.395 / 31 = 0.045 exactly -> half-up 0.05, half-even 0.04
    assert convert(Decimal("1.395"), 2026, 1).daily_cost == Decimal("0.05")


def test_zero_converts_to_zero():
    c = convert(Decimal("0.00"), 2026, 4)
    assert c.daily_cost == Decimal("0.00")
    assert c.residual == Decimal("0.00")


def test_daily_cost_always_has_two_decimal_places():
    c = convert(Decimal("3100.00"), 2026, 3)
    assert c.daily_cost == Decimal("100.00")
    assert str(c.daily_cost) == "100.00"


def test_residual_is_bounded_by_half_a_cent_per_day():
    for month in range(1, 13):
        c = convert(Decimal("12345.67"), 2026, month)
        limit = Decimal("0.005") * c.days_in_month
        assert abs(c.residual) <= limit


def test_convert_rejects_float_input():
    with pytest.raises(TypeError):
        convert(5000.0, 2026, 2)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5000", Decimal("5000")),
        ("5,000.00", Decimal("5000.00")),
        ("$5,000.00", Decimal("5000.00")),
        (" $1,234.56 ", Decimal("1234.56")),
        ("(250.00)", Decimal("-250.00")),
        (1250, Decimal("1250")),
        (1250.5, Decimal("1250.5")),
        (Decimal("99.99"), Decimal("99.99")),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_money.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.money'`

- [ ] **Step 3: Write money.py**

`src/st_cost_uploader/money.py`:

```python
"""The money path. Decimal only, never float."""

from __future__ import annotations

import calendar
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from st_cost_uploader.models import CostConversion

CENTS = Decimal("0.01")


def days_in_month(year: int, month: int) -> int:
    """Days in the given month. Leap years come from the standard library."""
    return calendar.monthrange(year, month)[1]


def convert(monthly_total: Decimal, year: int, month: int) -> CostConversion:
    """Convert a monthly total into the daily cost ServiceTitan stores.

    ServiceTitan holds dailyCost to the cent, so the reconstructed monthly
    total will not always equal the input. The residual is returned rather
    than hidden.
    """
    if not isinstance(monthly_total, Decimal):
        raise TypeError(
            f"monthly_total must be Decimal, got {type(monthly_total).__name__}. "
            "Float input would silently corrupt the money path."
        )

    days = days_in_month(year, month)
    daily = (monthly_total / Decimal(days)).quantize(CENTS, rounding=ROUND_HALF_UP)
    reconstructed = (daily * Decimal(days)).quantize(CENTS, rounding=ROUND_HALF_UP)
    residual = (reconstructed - monthly_total).quantize(CENTS, rounding=ROUND_HALF_UP)

    return CostConversion(
        monthly_total=monthly_total,
        days_in_month=days,
        daily_cost=daily,
        reconstructed_total=reconstructed,
        residual=residual,
    )


def parse_money(value: object) -> Decimal | None:
    """Coerce a spreadsheet cell into Decimal. Returns None when not a number.

    Floats are routed through str() so that 1250.5 becomes Decimal("1250.5")
    rather than the binary expansion.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("$", "").replace(",", "").replace(" ", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_money.py -v
```

Expected: all passed. If `test_rounding_is_half_up_not_bankers` fails, the `rounding=ROUND_HALF_UP` argument is missing somewhere; Python's default is `ROUND_HALF_EVEN`.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/money.py tests/test_money.py
git commit -m "feat: add Decimal money conversion with residual tracking"
```

---

### Task 4: The spreadsheet parser

**Files:**
- Create: `src/st_cost_uploader/parser.py`
- Test: `tests/test_parser.py`
- Create: `tests/fixtures/__init__.py` (empty, so fixtures are importable)

**Interfaces:**
- Consumes: `SheetRow`, `ParseError`, `ParseResult` from Task 2; `parse_money` from Task 3
- Produces: `parse_period(value) -> tuple[int, int] | None`, `detect_layout(headers) -> str`, `parse_workbook(data: bytes, filename: str, layout: str | None = None) -> ParseResult`

- [ ] **Step 1: Write the failing test**

`tests/test_parser.py`:

```python
import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from st_cost_uploader.parser import detect_layout, parse_period, parse_workbook


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-02", (2026, 2)),
        ("2026/2", (2026, 2)),
        ("2026-02-01", (2026, 2)),
        ("02-2026", (2026, 2)),
        ("Feb 2026", (2026, 2)),
        ("February 2026", (2026, 2)),
        ("feb 2026", (2026, 2)),
        ("Jan, 2026", (2026, 1)),
        (date(2026, 2, 14), (2026, 2)),
        ("2026-13", None),
        ("Campaign", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_period(raw, expected):
    assert parse_period(raw) == expected


def test_detect_layout_long():
    assert detect_layout(["Campaign", "Month", "Spend"]) == "long"


def test_detect_layout_wide():
    assert detect_layout(["Campaign", "Jan 2026", "Feb 2026", "Mar 2026"]) == "wide"


def test_detect_layout_wide_needs_two_periods():
    # A single period-looking column is not enough to call it wide.
    assert detect_layout(["Campaign", "Feb 2026", "Notes"]) == "long"


def test_parse_long_layout():
    data = _xlsx([
        ["Campaign", "Month", "Spend"],
        ["Google - Search Brand", "2026-02", "5,000.00"],
        ["Yelp Ads", "2026-03", "$1,860.00"],
    ])
    result = parse_workbook(data, "spend.xlsx")

    assert result.layout == "long"
    assert result.errors == ()
    assert len(result.rows) == 2
    assert result.rows[0].campaign_name == "Google - Search Brand"
    assert result.rows[0].year == 2026
    assert result.rows[0].month == 2
    assert result.rows[0].monthly_total == Decimal("5000.00")
    assert result.rows[1].monthly_total == Decimal("1860.00")


def test_parse_wide_layout_unpivots():
    data = _xlsx([
        ["Campaign", "Jan 2026", "Feb 2026"],
        ["Yelp", "1000", "2000"],
        ["Nextdoor", "500", ""],
    ])
    result = parse_workbook(data, "spend.xlsx")

    assert result.layout == "wide"
    assert len(result.rows) == 3  # blank cell is skipped, not an error
    assert (result.rows[0].campaign_name, result.rows[0].month) == ("Yelp", 1)
    assert result.rows[1].monthly_total == Decimal("2000")
    assert (result.rows[2].campaign_name, result.rows[2].month) == ("Nextdoor", 1)


def test_manual_layout_override_beats_detection():
    # Headers look long, but the caller insists on wide.
    data = _xlsx([
        ["Campaign", "Jan 2026", "Notes"],
        ["Yelp", "1000", "ignore me"],
    ])
    result = parse_workbook(data, "spend.xlsx", layout="wide")

    assert result.layout == "wide"
    assert len(result.rows) == 1
    assert result.rows[0].month == 1


def test_bad_rows_become_errors_not_exceptions():
    data = _xlsx([
        ["Campaign", "Month", "Spend"],
        ["Good", "2026-02", "100"],
        ["", "2026-02", "100"],
        ["Bad Period", "not a date", "100"],
        ["Bad Amount", "2026-02", "n/a"],
    ])
    result = parse_workbook(data, "spend.xlsx")

    assert len(result.rows) == 1
    assert len(result.errors) == 3
    assert {e.source_row for e in result.errors} == {3, 4, 5}
    assert "campaign name" in result.errors[0].message.lower()


def test_column_mapping_is_reported():
    data = _xlsx([
        ["Campaign", "Month", "Spend"],
        ["Yelp", "2026-02", "100"],
    ])
    result = parse_workbook(data, "spend.xlsx")
    assert result.column_mapping == {
        "Campaign": "campaign_name",
        "Month": "period",
        "Spend": "monthly_total",
    }


def test_csv_is_supported():
    csv = b"Campaign,Month,Spend\nYelp,2026-02,1860.00\n"
    result = parse_workbook(csv, "spend.csv")
    assert result.layout == "long"
    assert result.rows[0].monthly_total == Decimal("1860.00")


def test_empty_file_raises():
    from st_cost_uploader.parser import ParserError

    with pytest.raises(ParserError):
        parse_workbook(_xlsx([]), "empty.xlsx")


def test_long_layout_missing_required_column_raises():
    from st_cost_uploader.parser import ParserError

    data = _xlsx([["Campaign", "Notes"], ["Yelp", "hi"]])
    with pytest.raises(ParserError):
        parse_workbook(data, "spend.xlsx")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_parser.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.parser'`

- [ ] **Step 3: Write parser.py**

`src/st_cost_uploader/parser.py`:

```python
"""Spreadsheet reading and layout normalization.

Two layouts are accepted:
  long: one row per campaign-month, with campaign / period / amount columns
  wide: campaigns down the left, one column per month

Both normalize to SheetRow. Detection is reported to the caller so the UI can
show it and offer an override, because a misdetected layout must never be
silent.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
from datetime import date

from openpyxl import load_workbook

from st_cost_uploader.models import ParseError, ParseResult, SheetRow
from st_cost_uploader.money import parse_money

MIN_PERIOD_COLUMNS_FOR_WIDE = 2

_MONTHS: dict[str, int] = {}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i

_CAMPAIGN_PATTERN = re.compile(r"campaign|name", re.I)
_PERIOD_PATTERN = re.compile(r"month|period|date", re.I)
_AMOUNT_PATTERN = re.compile(r"spend|cost|total|amount|budget", re.I)


class ParserError(Exception):
    """The file cannot be read at all."""


def parse_period(value: object) -> tuple[int, int] | None:
    """Coerce a cell into (year, month). Returns None when it is not a period."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.year, value.month

    text = str(value).strip()
    if not text:
        return None

    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        return (year, month) if 1 <= month <= 12 else None

    m = re.fullmatch(r"(\d{1,2})[-/](\d{4})", text)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        return (year, month) if 1 <= month <= 12 else None

    m = re.fullmatch(r"([A-Za-z]+)[\s,\-]+(\d{4})", text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return int(m.group(2)), month

    return None


def detect_layout(headers: list[object]) -> str:
    """Wide when two or more headers parse as periods, otherwise long."""
    period_columns = sum(1 for h in headers[1:] if parse_period(h) is not None)
    return "wide" if period_columns >= MIN_PERIOD_COLUMNS_FOR_WIDE else "long"


def _read_rows(data: bytes, filename: str) -> list[list[object]]:
    if filename.lower().endswith(".csv"):
        text = data.decode("utf-8-sig")
        return [list(r) for r in csv.reader(io.StringIO(text))]

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheet = workbook.active
    return [list(r) for r in sheet.iter_rows(values_only=True)]


def _find_column(headers: list[object], pattern: re.Pattern[str]) -> int | None:
    for index, header in enumerate(headers):
        if header is not None and pattern.search(str(header)):
            return index
    return None


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _parse_long(
    headers: list[object], body: list[list[object]]
) -> tuple[list[SheetRow], list[ParseError], dict[str, str]]:
    campaign_col = _find_column(headers, _CAMPAIGN_PATTERN)
    period_col = _find_column(headers, _PERIOD_PATTERN)
    amount_col = _find_column(headers, _AMOUNT_PATTERN)

    missing = [
        label
        for label, col in (
            ("campaign", campaign_col), ("period", period_col), ("amount", amount_col)
        )
        if col is None
    ]
    if missing:
        raise ParserError(
            f"Long layout needs a {', '.join(missing)} column. "
            f"Found headers: {[_clean(h) for h in headers if _clean(h)]}"
        )

    mapping = {
        _clean(headers[campaign_col]): "campaign_name",
        _clean(headers[period_col]): "period",
        _clean(headers[amount_col]): "monthly_total",
    }

    rows: list[SheetRow] = []
    errors: list[ParseError] = []
    for offset, raw in enumerate(body):
        line = offset + 2
        name = _clean(raw[campaign_col] if campaign_col < len(raw) else None)
        period = parse_period(raw[period_col] if period_col < len(raw) else None)
        amount = parse_money(raw[amount_col] if amount_col < len(raw) else None)

        if not name and period is None and amount is None:
            continue
        if not name:
            errors.append(ParseError(line, "Missing campaign name"))
            continue
        if period is None:
            errors.append(ParseError(line, f"Could not read a year and month for '{name}'"))
            continue
        if amount is None:
            errors.append(ParseError(line, f"Could not read an amount for '{name}'"))
            continue

        rows.append(SheetRow(name, period[0], period[1], amount, line))

    return rows, errors, mapping


def _parse_wide(
    headers: list[object], body: list[list[object]]
) -> tuple[list[SheetRow], list[ParseError], dict[str, str]]:
    periods = [(i, p) for i, h in enumerate(headers) if (p := parse_period(h)) is not None]
    if not periods:
        raise ParserError(
            "Wide layout needs at least one column header that reads as a month, "
            f"such as 'Feb 2026'. Found: {[_clean(h) for h in headers if _clean(h)]}"
        )

    mapping = {_clean(headers[0]): "campaign_name"}
    for index, (year, month) in periods:
        mapping[_clean(headers[index])] = f"period:{year}-{month:02d}"

    rows: list[SheetRow] = []
    errors: list[ParseError] = []
    for offset, raw in enumerate(body):
        line = offset + 2
        name = _clean(raw[0] if raw else None)
        if not name:
            if any(_clean(c) for c in raw):
                errors.append(ParseError(line, "Missing campaign name"))
            continue

        for index, (year, month) in periods:
            cell = raw[index] if index < len(raw) else None
            if _clean(cell) == "":
                continue
            amount = parse_money(cell)
            if amount is None:
                errors.append(
                    ParseError(line, f"Could not read an amount for '{name}' in {year}-{month:02d}")
                )
                continue
            rows.append(SheetRow(name, year, month, amount, line))

    return rows, errors, mapping


def parse_workbook(data: bytes, filename: str, layout: str | None = None) -> ParseResult:
    """Read a spreadsheet into canonical rows.

    layout: pass "long" or "wide" to override detection. None auto-detects.
    """
    raw_rows = _read_rows(data, filename)
    raw_rows = [r for r in raw_rows if any(_clean(c) for c in r)]
    if not raw_rows:
        raise ParserError("The file has no rows.")

    headers, body = raw_rows[0], raw_rows[1:]
    chosen = layout or detect_layout(headers)
    if chosen not in ("long", "wide"):
        raise ParserError(f"Unknown layout '{chosen}'. Use 'long' or 'wide'.")

    if chosen == "wide":
        rows, errors, mapping = _parse_wide(headers, body)
    else:
        rows, errors, mapping = _parse_long(headers, body)

    return ParseResult(
        layout=chosen,
        rows=tuple(rows),
        errors=tuple(errors),
        column_mapping=mapping,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_parser.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/parser.py tests/test_parser.py
git commit -m "feat: parse long and wide spreadsheet layouts into canonical rows"
```

---

### Task 5: Name normalization and the alias store

**Files:**
- Create: `src/st_cost_uploader/normalize.py`
- Create: `src/st_cost_uploader/aliases.py`
- Test: `tests/test_aliases.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `normalize_name(value: str) -> str`, `AliasStore` with `.load(tenant, base_dir)`, `.get(sheet_name)`, `.set(sheet_name, campaign_id)`, `.save()`, `.as_dict()`

- [ ] **Step 1: Write the failing test**

`tests/test_aliases.py`:

```python
import json

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.normalize import normalize_name


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_name("  Google   Search  ") == "google search"
    assert normalize_name("YELP") == "yelp"
    assert normalize_name("Search||Google||Brand") == "search||google||brand"


def test_normalize_handles_non_breaking_space():
    assert normalize_name("Google Ads") == "google ads"


def test_load_creates_empty_store_when_file_absent(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    assert store.get("anything") is None
    assert store.as_dict() == {}


def test_set_then_get_roundtrips_through_disk(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    store.set("Yelp Ads", 1000001)
    store.save()

    reloaded = AliasStore.load("acme_east", tmp_path)
    assert reloaded.get("Yelp Ads") == 1000001


def test_lookup_is_normalized(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    store.set("Yelp   Ads", 99)
    assert store.get("yelp ads") == 99
    assert store.get("  YELP ADS ") == 99


def test_file_lands_at_expected_path_and_is_readable_json(tmp_path):
    store = AliasStore.load("northwind", tmp_path)
    store.set("Nextdoor - Local", 123)
    store.save()

    path = tmp_path / "northwind.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload == {"nextdoor - local": 123}


def test_tenants_do_not_share_aliases(tmp_path):
    a = AliasStore.load("acme_east", tmp_path)
    a.set("Yelp", 1)
    a.save()

    b = AliasStore.load("globex", tmp_path)
    assert b.get("Yelp") is None


def test_corrupt_file_raises_rather_than_silently_emptying(tmp_path):
    (tmp_path / "acme_east.json").write_text("{not json")
    try:
        AliasStore.load("acme_east", tmp_path)
    except ValueError as exc:
        assert "acme_east.json" in str(exc)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_aliases.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.aliases'`

- [ ] **Step 3: Write normalize.py and aliases.py**

`src/st_cost_uploader/normalize.py`:

```python
"""Shared name normalization. Used by both the alias store and the resolver
so that a name matched one way is matched the same way everywhere."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    """Lowercase and collapse whitespace. Nothing else.

    Deliberately conservative: campaign names carry meaningful punctuation
    such as 'Search||Google||Brand', so stripping symbols would merge
    campaigns that are genuinely distinct.
    """
    return _WHITESPACE.sub(" ", value.replace(" ", " ")).strip().lower()
```

`src/st_cost_uploader/aliases.py`:

```python
"""Per-tenant map from spreadsheet campaign name to ServiceTitan campaign ID.

Stored as JSON under aliases/<tenant>.json and tracked in git. It holds no
secrets, and sharing it means a name confirmed once is resolved for everyone.
"""

from __future__ import annotations

import json
from pathlib import Path

from st_cost_uploader.normalize import normalize_name

DEFAULT_DIR = Path("aliases")


class AliasStore:
    def __init__(self, path: Path, mapping: dict[str, int]) -> None:
        self._path = path
        self._mapping = mapping

    @classmethod
    def load(cls, tenant: str, base_dir: Path | str = DEFAULT_DIR) -> AliasStore:
        path = Path(base_dir) / f"{tenant}.json"
        if not path.exists():
            return cls(path, {})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return cls(path, {str(k): int(v) for k, v in payload.items()})

    def get(self, sheet_name: str) -> int | None:
        return self._mapping.get(normalize_name(sheet_name))

    def set(self, sheet_name: str, campaign_id: int) -> None:
        self._mapping[normalize_name(sheet_name)] = int(campaign_id)

    def as_dict(self) -> dict[str, int]:
        return dict(self._mapping)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(dict(sorted(self._mapping.items())), indent=2) + "\n"
        self._path.write_text(payload, encoding="utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_aliases.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/normalize.py src/st_cost_uploader/aliases.py tests/test_aliases.py
git commit -m "feat: add name normalization and per-tenant alias store"
```

---

### Task 6: The campaign resolver

**Files:**
- Create: `src/st_cost_uploader/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `SheetRow`, `Campaign`, `Resolution`, `MatchKind`, `Candidate` from Task 2; `normalize_name` from Task 5; `AliasStore` from Task 5
- Produces: `resolve(rows, campaigns, alias_store) -> list[Resolution]`, constants `FUZZY_FLOOR = 60`, `MAX_CANDIDATES = 5`

- [ ] **Step 1: Write the failing test**

`tests/test_resolver.py`:

```python
from decimal import Decimal

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.models import Campaign, MatchKind, SheetRow
from st_cost_uploader.resolver import FUZZY_FLOOR, MAX_CANDIDATES, resolve

CAMPAIGNS = [
    Campaign(id=1, name="Search||Google||Brand", active=True),
    Campaign(id=2, name="Facebook Retargeting", active=True),
    Campaign(id=3, name="Yelp", active=True),
    Campaign(id=4, name="Local Services Ads", active=True),
    Campaign(id=5, name="Angi Leads", active=False),
]


def _row(name: str) -> SheetRow:
    return SheetRow(name, 2026, 2, Decimal("100"), 2)


def _store(tmp_path, pairs=()):
    store = AliasStore.load("t", tmp_path)
    for name, cid in pairs:
        store.set(name, cid)
    return store


def test_exact_match(tmp_path):
    [res] = resolve([_row("Yelp")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.EXACT
    assert res.campaign_id == 3


def test_exact_match_ignores_case_and_spacing(tmp_path):
    [res] = resolve([_row("  facebook   retargeting ")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.EXACT
    assert res.campaign_id == 2


def test_alias_resolves_when_name_differs(tmp_path):
    store = _store(tmp_path, [("Yelp Ads", 3)])
    [res] = resolve([_row("Yelp Ads")], CAMPAIGNS, store)
    assert res.kind is MatchKind.ALIAS
    assert res.campaign_id == 3
    assert res.campaign_name == "Yelp"


def test_alias_beats_a_higher_scoring_fuzzy_candidate(tmp_path):
    # "Angi Leads" is an exact-ish string for campaign 5, but the alias says 3.
    store = _store(tmp_path, [("Angi Leads", 3)])
    [res] = resolve([_row("Angi Leads")], CAMPAIGNS, store)
    assert res.kind is MatchKind.ALIAS
    assert res.campaign_id == 3


def test_alias_pointing_at_a_missing_campaign_falls_through(tmp_path):
    store = _store(tmp_path, [("Ghost", 9999)])
    [res] = resolve([_row("Ghost")], CAMPAIGNS, store)
    assert res.kind is not MatchKind.ALIAS
    assert res.campaign_id is None


def test_fuzzy_offers_candidates_without_resolving(tmp_path):
    [res] = resolve([_row("Facebook Retarget")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.FUZZY
    assert res.campaign_id is None
    assert res.is_resolved is False
    assert res.candidates[0].campaign_id == 2
    assert res.candidates[0].score >= FUZZY_FLOOR


def test_candidates_are_sorted_by_score_descending(tmp_path):
    [res] = resolve([_row("Google Search Brand")], CAMPAIGNS, _store(tmp_path))
    scores = [c.score for c in res.candidates]
    assert scores == sorted(scores, reverse=True)


def test_candidates_are_capped(tmp_path):
    many = [Campaign(id=i, name=f"Google Campaign {i}", active=True) for i in range(20)]
    [res] = resolve([_row("Google Campaign")], many, _store(tmp_path))
    assert len(res.candidates) <= MAX_CANDIDATES


def test_no_match_below_floor(tmp_path):
    [res] = resolve([_row("zzzzzzzzzz qqqqqqqq")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.NONE
    assert res.candidates == ()


def test_inactive_campaigns_are_still_matchable(tmp_path):
    # Historical months legitimately target campaigns that are now off.
    [res] = resolve([_row("Angi Leads")], CAMPAIGNS, _store(tmp_path))
    assert res.campaign_id == 5


def test_duplicate_names_do_not_exact_match(tmp_path):
    dupes = [
        Campaign(id=10, name="Google", active=True),
        Campaign(id=11, name="Google", active=True),
    ]
    [res] = resolve([_row("Google")], dupes, _store(tmp_path))
    assert res.kind is MatchKind.FUZZY
    assert res.campaign_id is None
    assert {c.campaign_id for c in res.candidates} == {10, 11}


def test_every_row_gets_a_resolution(tmp_path):
    rows = [_row("Yelp"), _row("Unknown Thing"), _row("Facebook Retargeting")]
    assert len(resolve(rows, CAMPAIGNS, _store(tmp_path))) == 3
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_resolver.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.resolver'`

- [ ] **Step 3: Write resolver.py**

`src/st_cost_uploader/resolver.py`:

```python
"""Resolve spreadsheet campaign names to ServiceTitan campaign IDs.

Three tiers, in strict order:
  1. exact match on the normalized name
  2. the persisted alias map
  3. ranked fuzzy candidates, which are offered but never auto-applied

An alias outranks any fuzzy score because the alias records a human decision.
An ambiguous exact match (two campaigns sharing a name) is deliberately not
resolved; the operator picks.
"""

from __future__ import annotations

from collections import defaultdict

from rapidfuzz import fuzz

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.models import Campaign, Candidate, MatchKind, Resolution, SheetRow
from st_cost_uploader.normalize import normalize_name

FUZZY_FLOOR = 60
MAX_CANDIDATES = 5


def resolve(
    rows: list[SheetRow],
    campaigns: list[Campaign],
    alias_store: AliasStore,
) -> list[Resolution]:
    by_name: dict[str, list[Campaign]] = defaultdict(list)
    for campaign in campaigns:
        by_name[normalize_name(campaign.name)].append(campaign)
    by_id = {c.id: c for c in campaigns}

    seen: dict[str, Resolution] = {}
    results: list[Resolution] = []

    for row in rows:
        key = normalize_name(row.campaign_name)
        cached = seen.get(key)
        if cached is not None:
            results.append(
                Resolution(
                    row=row,
                    kind=cached.kind,
                    campaign_id=cached.campaign_id,
                    campaign_name=cached.campaign_name,
                    candidates=cached.candidates,
                )
            )
            continue

        resolution = _resolve_one(row, key, by_name, by_id, alias_store, campaigns)
        seen[key] = resolution
        results.append(resolution)

    return results


def _resolve_one(
    row: SheetRow,
    key: str,
    by_name: dict[str, list[Campaign]],
    by_id: dict[int, Campaign],
    alias_store: AliasStore,
    campaigns: list[Campaign],
) -> Resolution:
    exact = by_name.get(key, [])
    if len(exact) == 1:
        return Resolution(row, MatchKind.EXACT, exact[0].id, exact[0].name)

    alias_id = alias_store.get(row.campaign_name)
    if alias_id is not None and alias_id in by_id:
        target = by_id[alias_id]
        return Resolution(row, MatchKind.ALIAS, target.id, target.name)

    candidates = _rank(row.campaign_name, campaigns)
    if candidates:
        return Resolution(row, MatchKind.FUZZY, None, None, candidates)

    return Resolution(row, MatchKind.NONE, None, None, ())


def _rank(sheet_name: str, campaigns: list[Campaign]) -> tuple[Candidate, ...]:
    normalized = normalize_name(sheet_name)
    scored: list[Candidate] = []
    for campaign in campaigns:
        score = int(round(fuzz.token_set_ratio(normalized, normalize_name(campaign.name))))
        if score >= FUZZY_FLOOR:
            scored.append(Candidate(campaign.id, campaign.name, score))

    scored.sort(key=lambda c: (-c.score, c.campaign_name))
    return tuple(scored[:MAX_CANDIDATES])
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_resolver.py -v
```

Expected: all passed. If `test_no_match_below_floor` fails, `token_set_ratio` is scoring unrelated strings above 60; check that both sides are being normalized.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/resolver.py tests/test_resolver.py
git commit -m "feat: resolve campaign names via exact, alias, then fuzzy tiers"
```

---

### Task 7: Configuration loading

**Files:**
- Create: `src/st_cost_uploader/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `TenantConfig` dataclass with `name`, `tenant_id`, `client_id`, `client_secret`, `app_key`; `load_tenants(env: Mapping[str, str]) -> dict[str, TenantConfig]`; `ConfigError`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:

```python
import pytest

from st_cost_uploader.config import ConfigError, load_tenants

BASE = {
    "ST_TENANTS": "acme_east,northwind",
    "ST_TENANT_ACME_EAST_ID": "100000001",
    "ST_TENANT_ACME_EAST_CLIENT_ID": "cid.aaa",
    "ST_TENANT_ACME_EAST_CLIENT_SECRET": "cs2.bbb",
    "ST_TENANT_ACME_EAST_APP_KEY": "ak1.ccc",
    "ST_TENANT_NORTHWIND_ID": "111",
    "ST_TENANT_NORTHWIND_CLIENT_ID": "cid.ddd",
    "ST_TENANT_NORTHWIND_CLIENT_SECRET": "cs2.eee",
    "ST_TENANT_NORTHWIND_APP_KEY": "ak1.fff",
}


def test_loads_every_listed_tenant():
    tenants = load_tenants(BASE)
    assert set(tenants) == {"acme_east", "northwind"}
    assert tenants["acme_east"].tenant_id == "100000001"
    assert tenants["northwind"].app_key == "ak1.fff"


def test_whitespace_in_the_tenant_list_is_tolerated():
    env = dict(BASE, ST_TENANTS=" acme_east , northwind ")
    assert set(load_tenants(env)) == {"acme_east", "northwind"}


def test_missing_tenant_list_raises():
    with pytest.raises(ConfigError, match="ST_TENANTS"):
        load_tenants({})


def test_missing_credential_names_the_variable():
    env = dict(BASE)
    del env["ST_TENANT_NORTHWIND_CLIENT_SECRET"]
    with pytest.raises(ConfigError, match="ST_TENANT_NORTHWIND_CLIENT_SECRET"):
        load_tenants(env)


def test_blank_credential_is_treated_as_missing():
    env = dict(BASE, ST_TENANT_NORTHWIND_APP_KEY="   ")
    with pytest.raises(ConfigError, match="ST_TENANT_NORTHWIND_APP_KEY"):
        load_tenants(env)


def test_repr_does_not_leak_the_secret():
    tenants = load_tenants(BASE)
    assert "cs2.bbb" not in repr(tenants["acme_east"])
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.config'`

- [ ] **Step 3: Write config.py**

`src/st_cost_uploader/config.py`:

```python
"""Credential loading. Mirrors the variable naming used by the
servicetitan-local MCP server so one .env can serve both."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuration is missing or incomplete."""


@dataclass(frozen=True)
class TenantConfig:
    name: str
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    app_key: str = field(repr=False)


def _require(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"{key} is missing or blank. See .env.example.")
    return value


def load_tenants(env: Mapping[str, str] | None = None) -> dict[str, TenantConfig]:
    """Build one TenantConfig per slug listed in ST_TENANTS."""
    if env is None:
        load_dotenv()
        env = os.environ

    raw = (env.get("ST_TENANTS") or "").strip()
    if not raw:
        raise ConfigError("ST_TENANTS is missing or blank. See .env.example.")

    tenants: dict[str, TenantConfig] = {}
    for slug in (s.strip() for s in raw.split(",")):
        if not slug:
            continue
        prefix = f"ST_TENANT_{slug.upper()}"
        tenants[slug] = TenantConfig(
            name=slug,
            tenant_id=_require(env, f"{prefix}_ID"),
            client_id=_require(env, f"{prefix}_CLIENT_ID"),
            client_secret=_require(env, f"{prefix}_CLIENT_SECRET"),
            app_key=_require(env, f"{prefix}_APP_KEY"),
        )

    if not tenants:
        raise ConfigError("ST_TENANTS listed no usable tenant names.")
    return tenants
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/config.py tests/test_config.py
git commit -m "feat: load per-tenant ServiceTitan credentials from env"
```

---

### Task 8: ServiceTitan client, authentication and reads

**Files:**
- Create: `src/st_cost_uploader/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `TenantConfig` from Task 7; `Campaign`, `CostRecord` from Task 2
- Produces: `ServiceTitanClient` with `async list_campaigns()`, `async list_costs_for_campaigns(ids)`, `async get_cost(cost_id)`; module constants `TOKEN_URL`, `API_BASE`; `ServiceTitanError`

All HTTP is async via `httpx`. Tests inject a `httpx.MockTransport` so nothing touches the network.

- [ ] **Step 1: Write the failing test**

`tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_client.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.client'`

- [ ] **Step 3: Write client.py**

`src/st_cost_uploader/client.py`:

```python
"""Async ServiceTitan Marketing v2 client.

Endpoints and header names are copied from the verified reference table in
the implementation plan. All money crossing this boundary becomes Decimal
immediately, via str(), so no float ever reaches the money path.
"""

from __future__ import annotations

import time
from decimal import Decimal

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

    async def __aenter__(self) -> ServiceTitanClient:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_client.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/client.py tests/test_client.py
git commit -m "feat: add async ServiceTitan client with auth and paginated reads"
```

---

### Task 9: ServiceTitan client writes

**Depends on Task 1.** Use the verb Task 1 confirmed. The code below assumes `PATCH /costs/{id}`; if Task 1 found `PUT`, change the verb and the body in `update_cost` and in the corresponding test, and nothing else.

**Files:**
- Modify: `src/st_cost_uploader/client.py` (append two methods)
- Modify: `tests/test_client.py` (append tests)

**Interfaces:**
- Consumes: `ServiceTitanClient` from Task 8
- Produces: `async create_cost(campaign_id, year, month, daily_cost) -> int`, `async update_cost(cost_id, campaign_id, year, month, daily_cost) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_client.py`:

```python
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
    assert '"campaignId": 7' in seen["body"]
    assert '"year": 2026' in seen["body"]
    assert '"month": 2' in seen["body"]
    assert "178.57" in seen["body"]


async def test_daily_cost_is_serialized_as_a_number_not_a_string():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id": 1})

    await _client(handler).create_cost(7, 2026, 2, Decimal("178.57"))

    assert '"dailyCost": 178.57' in seen["body"]
    assert '"dailyCost": "178.57"' not in seen["body"]


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


async def test_write_failure_raises_with_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.servicetitan.io":
            return _token_response()
        return httpx.Response(422, text="validation failed")

    with pytest.raises(ServiceTitanError, match="422"):
        await _client(handler).create_cost(7, 2026, 2, Decimal("1.00"))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_client.py -k "create_cost or update_cost or serialized or write_failure" -v
```

Expected: FAIL with `AttributeError: 'ServiceTitanClient' object has no attribute 'create_cost'`

- [ ] **Step 3: Add the write methods**

Append to `src/st_cost_uploader/client.py`, inside the `ServiceTitanClient` class:

```python
    # ---- writes ---------------------------------------------------------

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
        return int(payload.get("id", 0))

    async def update_cost(
        self, cost_id: int, campaign_id: int, year: int, month: int, daily_cost: Decimal
    ) -> None:
        await self._request(
            "PATCH",
            self._tenant_path(f"costs/{cost_id}"),
            json_body=self._cost_body(campaign_id, year, month, daily_cost),
        )
```

- [ ] **Step 4: Run the whole client suite to verify it passes**

```bash
uv run pytest tests/test_client.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/client.py tests/test_client.py
git commit -m "feat: add cost create and update to the ServiceTitan client"
```

---

### Task 10: The planner

**Files:**
- Create: `src/st_cost_uploader/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `Resolution`, `CostRecord`, `PlannedWrite`, `Action`, `PeriodKey` from Task 2; `convert` from Task 3
- Produces: `plan(resolutions, existing_costs) -> tuple[list[PlannedWrite], list[Resolution]]` returning planned writes and the unresolved resolutions

- [ ] **Step 1: Write the failing test**

`tests/test_planner.py`:

```python
from decimal import Decimal

from st_cost_uploader.models import (
    Action, CostRecord, MatchKind, Resolution, SheetRow,
)
from st_cost_uploader.planner import plan


def _resolution(name="Yelp", cid=3, year=2026, month=3, total="1860.00", kind=MatchKind.EXACT):
    row = SheetRow(name, year, month, Decimal(total), 2)
    return Resolution(row, kind, cid, name)


def test_absent_record_becomes_create():
    [written], unresolved = plan([_resolution()], {})

    assert written.action is Action.CREATE
    assert written.cost_id is None
    assert written.prior_daily_cost is None
    assert written.conversion.daily_cost == Decimal("60.00")
    assert unresolved == []


def test_existing_record_with_different_value_becomes_update():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("50.00")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.UPDATE
    assert written.cost_id == 77
    assert written.prior_daily_cost == Decimal("50.00")


def test_existing_record_with_the_same_value_becomes_no_change():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("60.00")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.NO_CHANGE
    assert written.cost_id == 77


def test_existing_zero_record_becomes_update_not_create():
    # ServiceTitan pre-creates some rows at 0.0. Those must be updated,
    # never created again.
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("0.0")),
    }
    [written], _ = plan([_resolution()], existing)

    assert written.action is Action.UPDATE
    assert written.cost_id == 77
    assert written.overwrites_nonzero is False


def test_overwrites_nonzero_flags_only_real_replacements():
    existing = {
        (3, 2026, 3): CostRecord(id=77, campaign_id=3, year=2026, month=3,
                                 daily_cost=Decimal("50.00")),
    }
    [written], _ = plan([_resolution()], existing)
    assert written.overwrites_nonzero is True


def test_unresolved_rows_are_returned_separately_and_never_planned():
    row = SheetRow("Mystery", 2026, 3, Decimal("100"), 4)
    unresolved_input = Resolution(row, MatchKind.NONE, None, None)

    written, unresolved = plan([_resolution(), unresolved_input], {})

    assert len(written) == 1
    assert len(unresolved) == 1
    assert unresolved[0].row.campaign_name == "Mystery"


def test_leap_year_divisor_is_used():
    [written], _ = plan([_resolution(year=2024, month=2, total="7000.00")], {})

    assert written.conversion.days_in_month == 29
    assert written.conversion.daily_cost == Decimal("241.38")
    assert written.conversion.residual == Decimal("0.02")


def test_later_duplicate_row_wins():
    # The same campaign-month appearing twice in one sheet is a user error,
    # but it must not produce two conflicting writes.
    first = _resolution(total="1000.00")
    second = _resolution(total="1860.00")

    written, _ = plan([first, second], {})

    assert len(written) == 1
    assert written[0].conversion.monthly_total == Decimal("1860.00")


def test_sheet_name_is_preserved_for_the_preview():
    row = SheetRow("Yelp Ads", 2026, 3, Decimal("1860.00"), 2)
    res = Resolution(row, MatchKind.ALIAS, 3, "Yelp")

    [written], _ = plan([res], {})

    assert written.sheet_name == "Yelp Ads"
    assert written.campaign_name == "Yelp"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_planner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.planner'`

- [ ] **Step 3: Write planner.py**

`src/st_cost_uploader/planner.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_planner.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/planner.py tests/test_planner.py
git commit -m "feat: plan create, update, and no-change per campaign-month"
```

---

### Task 11: The audit log

**Files:**
- Create: `src/st_cost_uploader/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `WriteOutcome`, `Action` from Task 2
- Produces: `AuditLog(path)` with `.record(tenant, outcome)` and `.record_many(tenant, outcomes)`

- [ ] **Step 1: Write the failing test**

`tests/test_audit.py`:

```python
import json
from decimal import Decimal

from st_cost_uploader.audit import AuditLog
from st_cost_uploader.models import (
    Action, CostConversion, PlannedWrite, WriteOutcome,
)


def _outcome(ok=True, error=None, action=Action.UPDATE, prior="50.00") -> WriteOutcome:
    conv = CostConversion(
        monthly_total=Decimal("1860.00"), days_in_month=31,
        daily_cost=Decimal("60.00"), reconstructed_total=Decimal("1860.00"),
        residual=Decimal("0.00"),
    )
    plan = PlannedWrite(
        campaign_id=3, campaign_name="Yelp", sheet_name="Yelp Ads",
        year=2026, month=3, action=action, conversion=conv,
        cost_id=77, prior_daily_cost=Decimal(prior) if prior else None,
    )
    return WriteOutcome(plan=plan, ok=ok, error=error)


def test_record_writes_one_json_line(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("acme_east", _outcome())

    lines = (tmp_path / "writes.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["tenant"] == "acme_east"
    assert entry["campaign_id"] == 3
    assert entry["campaign_name"] == "Yelp"
    assert entry["sheet_name"] == "Yelp Ads"
    assert entry["year"] == 2026
    assert entry["month"] == 3
    assert entry["action"] == "update"
    assert entry["prior_daily_cost"] == "50.00"
    assert entry["new_daily_cost"] == "60.00"
    assert entry["monthly_total"] == "1860.00"
    assert entry["residual"] == "0.00"
    assert entry["ok"] is True
    assert entry["error"] is None
    assert "timestamp" in entry


def test_money_is_stored_as_a_string_to_survive_json(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert isinstance(entry["new_daily_cost"], str)


def test_appends_rather_than_truncates(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())
    log.record("t", _outcome())

    assert len((tmp_path / "writes.jsonl").read_text().strip().splitlines()) == 2


def test_creates_the_parent_directory(tmp_path):
    log = AuditLog(tmp_path / "nested" / "deeper" / "writes.jsonl")
    log.record("t", _outcome())

    assert (tmp_path / "nested" / "deeper" / "writes.jsonl").exists()


def test_failures_are_recorded_with_the_error(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome(ok=False, error="HTTP 422: validation failed"))

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert entry["ok"] is False
    assert "422" in entry["error"]


def test_create_records_a_null_prior(tmp_path):
    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome(action=Action.CREATE, prior=None))

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    assert entry["action"] == "create"
    assert entry["prior_daily_cost"] is None


def test_timestamp_is_utc_iso8601(tmp_path):
    from datetime import datetime

    log = AuditLog(tmp_path / "writes.jsonl")
    log.record("t", _outcome())

    entry = json.loads((tmp_path / "writes.jsonl").read_text().strip())
    parsed = datetime.fromisoformat(entry["timestamp"])
    assert parsed.tzinfo is not None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.audit'`

- [ ] **Step 3: Write audit.py**

`src/st_cost_uploader/audit.py`:

```python
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
            "cost_id": plan.cost_id,
            "monthly_total": str(plan.conversion.monthly_total),
            "prior_daily_cost": None if prior is None else str(prior),
            "new_daily_cost": str(plan.conversion.daily_cost),
            "residual": str(plan.conversion.residual),
            "ok": outcome.ok,
            "error": outcome.error,
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/st_cost_uploader/audit.py tests/test_audit.py
git commit -m "feat: add append-only JSONL audit log for writes"
```

---

### Task 12: The writer

**Files:**
- Create: `src/st_cost_uploader/writer.py`
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `PlannedWrite`, `Action`, `WriteOutcome` from Task 2; `ServiceTitanClient` from Tasks 8 and 9; `AuditLog` from Task 11
- Produces: `async execute(plans, client, audit, tenant) -> list[WriteOutcome]`; constant `MAX_CONCURRENCY = 8`

- [ ] **Step 1: Write the failing test**

`tests/test_writer.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_writer.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.writer'`

- [ ] **Step 3: Write writer.py**

`src/st_cost_uploader/writer.py`:

```python
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
                    await client.create_cost(
                        plan.campaign_id, plan.year, plan.month, plan.conversion.daily_cost
                    )
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_writer.py -v
```

Expected: all passed.

- [ ] **Step 5: Run the whole engine suite**

```bash
uv run pytest -v
```

Expected: every test from Tasks 2 through 12 passes. The engine is now complete and the web layer is the only thing left.

- [ ] **Step 6: Commit**

```bash
git add src/st_cost_uploader/writer.py tests/test_writer.py
git commit -m "feat: execute approved plans with bounded concurrency and audit"
```

---

### Task 13: Web app skeleton and the upload screen

Visual reference: the "1 Upload" frame in `st_cost_updater.pen`. Match the palette and layout; the CSS tokens below come from that file.

**Files:**
- Create: `src/st_cost_uploader/web/__init__.py`
- Create: `src/st_cost_uploader/web/app.py`
- Create: `src/st_cost_uploader/web/templates/base.html`
- Create: `src/st_cost_uploader/web/templates/upload.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `load_tenants` from Task 7; `parse_workbook`, `ParserError` from Task 4
- Produces: `app` (FastAPI), `SESSIONS: dict[str, UploadSession]`, `UploadSession` dataclass with `id`, `tenant`, `filename`, `parse_result`, and mutable `resolutions`, `plans`, `unresolved`, `outcomes`

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_web.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'st_cost_uploader.web'`

- [ ] **Step 3: Write base.html**

`src/st_cost_uploader/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaign Cost Uploader</title>
<script src="https://unpkg.com/htmx.org@2.0.3"></script>
<style>
:root{
  --bg:#F7F6F3; --surface:#FFF; --ink:#1A1917; --ink2:#6B6A63; --ink3:#9C9A92;
  --line:#E4E2DC; --accent:#2F5D50; --accent-soft:#E8F0ED;
  --warn:#B4552D; --warn-soft:#FBEFE8; --danger:#A33A2E;
  --font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:14px}
.bar{display:flex;justify-content:space-between;align-items:center;
     padding:16px 40px;background:var(--surface);border-bottom:1px solid var(--line)}
.brand{display:flex;gap:10px;align-items:center;font-weight:600;font-size:15px}
.steps{display:flex;gap:6px;align-items:center;color:var(--ink3);font-size:13px}
.step{padding:6px 13px;border-radius:999px}
.step.on{background:var(--accent);color:#fff;font-weight:500}
.body{padding:36px 40px;display:flex;flex-direction:column;gap:24px;max-width:1280px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--ink2);margin:0}
.label{font-size:11px;font-weight:600;letter-spacing:.8px;color:var(--ink3)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:24px}
.stats{display:flex;gap:12px}
.tile{flex:1;background:var(--surface);border:1px solid var(--line);
      border-radius:10px;padding:16px}
.tile b{display:block;font-family:var(--mono);font-size:26px;font-weight:600}
.tile span{color:var(--ink2);font-size:12px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{padding:9px 15px;border:1px solid var(--line);border-radius:8px;
      background:var(--surface);font-family:var(--mono);color:var(--ink2);cursor:pointer}
.chip input{display:none}
.chip:has(input:checked){background:var(--accent);border-color:var(--accent);
                          color:#fff;font-weight:600}
.drop{border:2px solid var(--line);border-radius:12px;background:var(--surface);
      padding:48px;text-align:center}
.drop b{display:block;font-size:16px;margin-bottom:4px}
.drop span{color:var(--ink3)}
table{width:100%;border-collapse:collapse;background:var(--surface);
      border:1px solid var(--line);border-radius:12px;overflow:hidden}
th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.8px;color:var(--ink3);
   background:var(--bg);padding:13px 20px;border-bottom:1px solid var(--line)}
td{padding:13px 20px;border-bottom:1px solid var(--line);font-family:var(--mono);color:var(--ink2)}
td.name{color:var(--ink)}
td.num,th.num{text-align:right}
tr.overwrite td{background:var(--warn-soft)}
tr.dim td{color:var(--ink3)}
.badge{display:inline-block;padding:4px 10px;border-radius:999px;
       font-family:var(--font);font-size:11px;font-weight:600}
.badge.create{background:var(--accent-soft);color:var(--accent)}
.badge.update{background:#F5E0D3;color:var(--warn)}
.badge.none{background:var(--bg);color:var(--ink3)}
.badge.alias{background:var(--accent-soft);color:var(--accent)}
.badge.fuzzy{background:var(--warn-soft);color:var(--warn)}
.badge.unmatched{background:#FBE9E7;color:var(--danger)}
.warnbar{display:flex;gap:10px;align-items:center;padding:13px 18px;border-radius:10px;
         background:var(--warn-soft);color:var(--warn);font-weight:500}
.foot{display:flex;justify-content:space-between;align-items:center}
.note{color:var(--ink3)}
button.primary{padding:11px 20px;border:0;border-radius:8px;background:var(--accent);
               color:#fff;font-family:var(--font);font-size:14px;font-weight:600;cursor:pointer}
button.ghost{padding:9px 14px;border:1px solid var(--line);border-radius:8px;
             background:var(--surface);color:var(--ink2);font-family:var(--font);cursor:pointer}
.err{color:var(--danger)}
select{font-family:var(--mono);font-size:13px;padding:8px 12px;
       border:1px solid var(--line);border-radius:8px;background:var(--surface)}
</style>
</head>
<body>
<div class="bar">
  <div class="brand">Campaign Cost Uploader</div>
  <div class="steps">
    <span class="step {% if step == 1 %}on{% endif %}">1 Upload</span>
    <span class="step {% if step == 2 %}on{% endif %}">2 Resolve</span>
    <span class="step {% if step == 3 %}on{% endif %}">3 Preview</span>
  </div>
</div>
<div class="body">{% block content %}{% endblock %}</div>
</body>
</html>
```

- [ ] **Step 4: Write upload.html**

`src/st_cost_uploader/web/templates/upload.html`:

```html
{% extends "base.html" %}
{% block content %}
<form method="post" action="/upload" enctype="multipart/form-data">
  <p class="label">TENANT</p>
  <div class="chips">
    {% for name in tenants %}
    <label class="chip">
      <input type="radio" name="tenant" value="{{ name }}"
             {% if loop.first %}checked{% endif %}>{{ name }}
    </label>
    {% endfor %}
  </div>

  {% if error %}<p class="err">{{ error }}</p>{% endif %}

  <div class="drop" style="margin-top:24px">
    <b>Choose a spreadsheet</b>
    <span>.xlsx or .csv &middot; long or wide layout &middot; up to about 1,000 rows</span>
    <p><input type="file" name="file" accept=".xlsx,.csv" required></p>
  </div>

  <p style="margin-top:20px">
    <label class="label" for="layout">LAYOUT</label>
    <select name="layout" id="layout">
      <option value="">Detect automatically</option>
      <option value="long">Force long: one row per campaign-month</option>
      <option value="wide">Force wide: months across the top</option>
    </select>
  </p>

  <div class="foot" style="margin-top:24px">
    <span class="note">Nothing is written to ServiceTitan until you approve the preview.</span>
    <button class="primary" type="submit">Resolve campaign names</button>
  </div>
</form>

{% if result %}
<div class="card">
  <p><b>{{ filename }}</b> &mdash; <span class="badge create">{{ result.layout|upper }} LAYOUT</span></p>
  <p class="sub">{{ result.rows|length }} rows &middot; {{ result.errors|length }} skipped</p>
  <table>
    <tr><th>SPREADSHEET COLUMN</th><th>MAPS TO</th></tr>
    {% for source, target in result.column_mapping.items() %}
    <tr><td class="name">{{ source }}</td><td>{{ target }}</td></tr>
    {% endfor %}
  </table>
  {% if result.errors %}
  <p class="label" style="margin-top:20px">SKIPPED ROWS</p>
  <table>
    <tr><th>ROW</th><th>REASON</th></tr>
    {% for e in result.errors %}
    <tr><td>{{ e.source_row }}</td><td>{{ e.message }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}
  <div class="foot" style="margin-top:20px">
    <span class="note">Wrong layout? Re-upload with the override set.</span>
    <form method="post" action="/resolve/{{ session_id }}">
      <button class="primary" type="submit">Resolve campaign names</button>
    </form>
  </div>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Write app.py**

`src/st_cost_uploader/web/app.py`:

```python
"""FastAPI layer. Thin: it holds session state and renders, and defers every
decision to the engine package."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from st_cost_uploader.config import ConfigError, load_tenants
from st_cost_uploader.models import ParseResult, PlannedWrite, Resolution, WriteOutcome
from st_cost_uploader.parser import ParserError, parse_workbook

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="ST Campaign Cost Uploader")


@dataclass
class UploadSession:
    id: str
    tenant: str
    filename: str
    parse_result: ParseResult
    resolutions: list[Resolution] = field(default_factory=list)
    plans: list[PlannedWrite] = field(default_factory=list)
    unresolved: list[Resolution] = field(default_factory=list)
    outcomes: list[WriteOutcome] = field(default_factory=list)


SESSIONS: dict[str, UploadSession] = {}


def _tenant_names() -> list[str]:
    try:
        return list(load_tenants())
    except ConfigError:
        return []


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "upload.html", {"step": 1, "tenants": _tenant_names()}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    tenant: str = Form(...),
    layout: str = Form(""),
    file: UploadFile = File(...),
) -> HTMLResponse:
    tenants = _tenant_names()

    def fail(message: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "upload.html",
            {"step": 1, "tenants": tenants, "error": message},
            status_code=400,
        )

    if tenant not in tenants:
        return fail(f"'{tenant}' is not a configured tenant.")

    try:
        result = parse_workbook(
            await file.read(), file.filename or "upload.xlsx", layout or None
        )
    except ParserError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - any unreadable file lands here
        return fail(f"Could not read that file: {exc}")

    session = UploadSession(
        id=uuid.uuid4().hex,
        tenant=tenant,
        filename=file.filename or "upload.xlsx",
        parse_result=result,
    )
    SESSIONS[session.id] = session

    return TEMPLATES.TemplateResponse(
        request,
        "upload.html",
        {
            "step": 1,
            "tenants": tenants,
            "result": result,
            "filename": session.filename,
            "session_id": session.id,
        },
    )
```

Create `src/st_cost_uploader/web/__init__.py` as an empty file.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/test_web.py -v
```

Expected: all passed.

- [ ] **Step 7: Start the app and look at it**

```bash
uv run uvicorn st_cost_uploader.web.app:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. Confirm the tenant chips render, one is selected, and the page matches the "1 Upload" frame in the Pencil file. Stop the server before continuing.

- [ ] **Step 8: Commit**

```bash
git add src/st_cost_uploader/web tests/test_web.py
git commit -m "feat: add web app skeleton and the upload screen"
```

---

### Task 14: The resolve screen

**Files:**
- Modify: `src/st_cost_uploader/web/app.py` (add `/resolve/{session_id}` GET and POST)
- Create: `src/st_cost_uploader/web/templates/resolve.html`
- Modify: `tests/test_web.py` (append tests)

**Interfaces:**
- Consumes: `UploadSession`, `SESSIONS` from Task 13; `resolve` from Task 6; `AliasStore` from Task 5; `ServiceTitanClient` from Task 8; `load_tenants` from Task 7
- Produces: `get_client(tenant_name) -> ServiceTitanClient` (module-level, monkeypatched in tests); routes `POST /resolve/{session_id}` and `POST /confirm/{session_id}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
from decimal import Decimal

from st_cost_uploader.models import Campaign, CostRecord


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
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    client.post(f"/confirm/{sid}", data={"choice_0": "skip"})

    assert SESSIONS[sid].resolutions[0].is_resolved is False


def test_unknown_session_returns_404(client, fake_st):
    assert client.post("/resolve/nope").status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_web.py -k "resolve or confirm or skipping or unknown_session" -v
```

Expected: FAIL with 404 or `AttributeError: module ... has no attribute 'get_client'`

- [ ] **Step 3: Write resolve.html**

`src/st_cost_uploader/web/templates/resolve.html`:

```html
{% extends "base.html" %}
{% block content %}
<div>
  <h1>Resolve campaign names</h1>
  <p class="sub">
    {{ total }} names in the sheet &middot; {{ auto }} resolved automatically
    &middot; {{ pending|length }} need your decision
  </p>
</div>

<div class="stats">
  <div class="tile"><b style="color:var(--accent)">{{ counts.exact }}</b><span>Exact name match</span></div>
  <div class="tile"><b style="color:var(--accent)">{{ counts.alias }}</b><span>Resolved from alias</span></div>
  <div class="tile"><b style="color:var(--warn)">{{ counts.fuzzy }}</b><span>Fuzzy suggestion</span></div>
  <div class="tile"><b style="color:var(--danger)">{{ counts.none }}</b><span>No match found</span></div>
</div>

<form method="post" action="/confirm/{{ session_id }}">
  {% if pending %}
  <table>
    <tr>
      <th>NAME IN SPREADSHEET</th><th>MATCH</th><th>SERVICETITAN CAMPAIGN</th>
    </tr>
    {% for index, res in pending %}
    <tr>
      <td class="name">{{ res.row.campaign_name }}</td>
      <td>
        {% if res.kind.value == 'fuzzy' %}
          <span class="badge fuzzy">{{ res.candidates[0].score }}%</span>
        {% else %}
          <span class="badge unmatched">UNMATCHED</span>
        {% endif %}
      </td>
      <td>
        <select name="choice_{{ index }}">
          <option value="skip">Skip this row</option>
          {% for c in res.candidates %}
          <option value="{{ c.campaign_id }}">{{ c.campaign_name }} ({{ c.score }}%)</option>
          {% endfor %}
          {% for c in all_campaigns %}
          <option value="{{ c.id }}">{{ c.name }}</option>
          {% endfor %}
        </select>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="note">Every name resolved. Nothing to decide here.</p>
  {% endif %}

  <div class="foot" style="margin-top:24px">
    <span class="note">
      Every confirmed match is saved to aliases/{{ tenant }}.json and reused on future uploads.
    </span>
    <button class="primary" type="submit">Build preview</button>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Add the routes to app.py**

Add these imports to the top of `src/st_cost_uploader/web/app.py`:

```python
from collections import Counter

from fastapi import HTTPException

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.audit import AuditLog
from st_cost_uploader.client import ServiceTitanClient
from st_cost_uploader.models import Campaign, MatchKind, Resolution
from st_cost_uploader.resolver import resolve as resolve_names
```

Add these module-level values just below `SESSIONS`:

```python
ALIAS_DIR = Path("aliases")
AUDIT_PATH = Path("logs") / "writes.jsonl"


def get_client(tenant_name: str) -> ServiceTitanClient:
    """Seam for tests. Monkeypatched to a stub so no test touches the network."""
    return ServiceTitanClient(load_tenants()[tenant_name])


def _session(session_id: str) -> UploadSession:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That upload session has expired.")
    return session


def _render_resolve(request: Request, session: UploadSession, campaigns: list[Campaign]):
    pending = [(i, r) for i, r in enumerate(session.resolutions) if not r.is_resolved]
    counts = Counter(r.kind.value for r in session.resolutions)
    return TEMPLATES.TemplateResponse(
        request,
        "resolve.html",
        {
            "step": 2,
            "session_id": session.id,
            "tenant": session.tenant,
            "total": len(session.resolutions),
            "auto": sum(1 for r in session.resolutions if r.is_resolved),
            "pending": pending,
            "counts": {
                "exact": counts.get("exact", 0),
                "alias": counts.get("alias", 0),
                "fuzzy": counts.get("fuzzy", 0),
                "none": counts.get("none", 0),
            },
            "all_campaigns": sorted(campaigns, key=lambda c: c.name),
        },
    )
```

Add the two routes at the end of the file:

```python
@app.post("/resolve/{session_id}", response_class=HTMLResponse)
async def resolve_screen(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    client = get_client(session.tenant)
    try:
        campaigns = await client.list_campaigns()
    finally:
        await client.aclose()

    store = AliasStore.load(session.tenant, ALIAS_DIR)
    session.resolutions = resolve_names(list(session.parse_result.rows), campaigns, store)
    session.campaigns = campaigns
    return _render_resolve(request, session, campaigns)


@app.post("/confirm/{session_id}", response_class=HTMLResponse)
async def confirm_choices(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    form = await request.form()
    store = AliasStore.load(session.tenant, ALIAS_DIR)
    by_id = {c.id: c for c in session.campaigns}

    for key, value in form.items():
        if not key.startswith("choice_") or value == "skip":
            continue
        index = int(key.removeprefix("choice_"))
        campaign = by_id.get(int(value))
        if campaign is None:
            continue
        current = session.resolutions[index]
        session.resolutions[index] = Resolution(
            row=current.row,
            kind=MatchKind.ALIAS,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
        )
        store.set(current.row.campaign_name, campaign.id)

    store.save()
    return await preview_screen(request, session_id)
```

Add `campaigns: list[Campaign] = field(default_factory=list)` to the `UploadSession` dataclass.

`preview_screen` does not exist yet, so Task 14's `/confirm` route will fail until Task 15 lands. That is expected and is why the confirm tests assert on session state and the alias file rather than on the response body.

- [ ] **Step 5: Add a temporary stub so Task 14's tests can run**

At the end of `app.py`, add:

```python
@app.post("/preview/{session_id}", response_class=HTMLResponse)
async def preview_screen(request: Request, session_id: str) -> HTMLResponse:
    _session(session_id)
    return HTMLResponse("preview pending")
```

Task 15 replaces this body.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/test_web.py -v
```

Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/st_cost_uploader/web tests/test_web.py
git commit -m "feat: add the campaign name resolution screen"
```

---

### Task 15: The preview screen

Visual reference: the "3 Preview" frame in `st_cost_updater.pen`.

**Files:**
- Modify: `src/st_cost_uploader/web/app.py` (replace the `preview_screen` stub)
- Create: `src/st_cost_uploader/web/templates/preview.html`
- Modify: `tests/test_web.py` (append tests)

**Interfaces:**
- Consumes: `plan` from Task 10; `get_client` from Task 14
- Produces: a real `POST /preview/{session_id}` that populates `session.plans` and `session.unresolved`, plus `GET /unmatched/{session_id}.csv`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
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

    assert "CREATE" in response.text.upper()
    assert SESSIONS[sid].plans[0].action.value == "create"


def test_preview_flags_an_overwrite_of_a_nonzero_value(client, fake_st):
    fake_st._costs = {
        (1, 2026, 2): CostRecord(id=99, campaign_id=1, year=2026, month=2,
                                 daily_cost=Decimal("71.43")),
    }
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    assert "71.43" in response.text
    assert "overwrite" in response.text.lower()
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


def test_preview_reports_the_net_residual(client, fake_st):
    sid = _start(client)
    client.post(f"/resolve/{sid}")
    response = client.post(f"/preview/{sid}")

    assert "residual" in response.text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_web.py -k "preview or unmatched" -v
```

Expected: FAIL because the stub returns "preview pending".

- [ ] **Step 3: Write preview.html**

`src/st_cost_uploader/web/templates/preview.html`:

```html
{% extends "base.html" %}
{% block content %}
<div>
  <h1>Review before writing</h1>
  <p class="sub">
    {{ filename }} &rarr; {{ tenant }}. Nothing has been sent to ServiceTitan yet.
  </p>
</div>

<div class="stats">
  <div class="tile"><b style="color:var(--accent)">{{ will_write }}</b><span>Rows will write</span></div>
  <div class="tile"><b style="color:var(--warn)">{{ overwrites }}</b><span>Overwrite existing value</span></div>
  <div class="tile"><b style="color:var(--danger)">{{ unresolved|length }}</b><span>Skipped, unmatched</span></div>
  <div class="tile"><b>{{ net_residual }}</b><span>Net rounding residual</span></div>
</div>

{% if overwrites %}
<div class="warnbar">
  {{ overwrites }} rows replace an existing non-zero cost. Those rows are tinted below,
  with the prior value shown.
</div>
{% endif %}

<table>
  <tr>
    <th>CAMPAIGN</th><th>PERIOD</th><th class="num">MONTHLY TOTAL</th>
    <th class="num">DAYS</th><th class="num">DAILY COST</th>
    <th class="num">RECONSTRUCTS</th><th class="num">&Delta;</th>
    <th class="num">PRIOR</th><th class="num">ACTION</th>
  </tr>
  {% for p in plans %}
  <tr class="{% if p.overwrites_nonzero %}overwrite{% elif p.action.value == 'no_change' %}dim{% endif %}">
    <td class="name">{{ p.campaign_name }}</td>
    <td>{{ p.year }}-{{ '%02d' % p.month }}</td>
    <td class="num">{{ p.conversion.monthly_total }}</td>
    <td class="num">{{ p.conversion.days_in_month }}</td>
    <td class="num"><b>{{ p.conversion.daily_cost }}</b></td>
    <td class="num">{{ p.conversion.reconstructed_total }}</td>
    <td class="num">{{ p.conversion.residual }}</td>
    <td class="num">{% if p.prior_daily_cost is none %}&mdash;{% else %}{{ p.prior_daily_cost }}{% endif %}</td>
    <td class="num"><span class="badge {{ p.action.value }}">{{ p.action.value|upper|replace('_',' ') }}</span></td>
  </tr>
  {% endfor %}
</table>

<p class="note">
  The divisor is computed per row. February 2024 divides by 29, February 2026 by 28.
  Residual is the cost of ServiceTitan storing dailyCost to the cent.
</p>

<div class="foot">
  {% if unresolved %}
  <a class="ghost" href="/unmatched/{{ session_id }}.csv">
    Download {{ unresolved|length }} unmatched rows
  </a>
  {% else %}<span class="note">Every row resolved.</span>{% endif %}
  <form method="post" action="/write/{{ session_id }}">
    <button class="primary" type="submit">
      Write {{ will_write }} costs to {{ tenant }}
    </button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 4: Replace the preview stub in app.py**

Add these imports:

```python
import csv as csv_module
import io as io_module
from decimal import Decimal

from fastapi.responses import StreamingResponse

from st_cost_uploader.models import Action
from st_cost_uploader.planner import plan as build_plan
```

Replace the whole `preview_screen` function with:

```python
@app.post("/preview/{session_id}", response_class=HTMLResponse)
async def preview_screen(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)

    resolved_ids = [r.campaign_id for r in session.resolutions if r.is_resolved]
    client = get_client(session.tenant)
    try:
        existing = await client.list_costs_for_campaigns(resolved_ids)
    finally:
        await client.aclose()

    session.plans, session.unresolved = build_plan(session.resolutions, existing)

    will_write = sum(1 for p in session.plans if p.action is not Action.NO_CHANGE)
    overwrites = sum(1 for p in session.plans if p.overwrites_nonzero)
    net = sum((p.conversion.residual for p in session.plans), Decimal("0.00"))

    return TEMPLATES.TemplateResponse(
        request,
        "preview.html",
        {
            "step": 3,
            "session_id": session.id,
            "tenant": session.tenant,
            "filename": session.filename,
            "plans": session.plans,
            "unresolved": session.unresolved,
            "will_write": will_write,
            "overwrites": overwrites,
            "net_residual": f"${net}",
        },
    )


@app.get("/unmatched/{session_id}.csv")
def unmatched_csv(session_id: str) -> StreamingResponse:
    session = _session(session_id)
    buffer = io_module.StringIO()
    writer = csv_module.writer(buffer)
    writer.writerow(["campaign_name", "year", "month", "monthly_total", "source_row"])
    for res in session.unresolved:
        row = res.row
        writer.writerow(
            [row.campaign_name, row.year, row.month, row.monthly_total, row.source_row]
        )
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="unmatched-{session_id}.csv"'},
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_web.py -v
```

Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/st_cost_uploader/web tests/test_web.py
git commit -m "feat: add the preview gate with residual and overwrite reporting"
```

---

### Task 16: Write execution and the results screen

**Files:**
- Modify: `src/st_cost_uploader/web/app.py` (add `POST /write/{session_id}`)
- Create: `src/st_cost_uploader/web/templates/results.html`
- Modify: `tests/test_web.py` (append tests)
- Modify: `README.md` (add a Running section)
- Modify: `next_steps.md` (mark the build complete)

**Interfaces:**
- Consumes: `execute` from Task 12; `AuditLog` from Task 11; `get_client`, `AUDIT_PATH` from Task 14
- Produces: `POST /write/{session_id}` rendering `results.html`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
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

    assert "1" in response.text
    assert "wrote" in response.text.lower() or "written" in response.text.lower()


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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_web.py -k write -v
```

Expected: FAIL with 405 Method Not Allowed, because `/write` does not exist.

- [ ] **Step 3: Write results.html**

`src/st_cost_uploader/web/templates/results.html`:

```html
{% extends "base.html" %}
{% block content %}
<div>
  <h1>{{ succeeded }} of {{ attempted }} written to {{ tenant }}</h1>
  <p class="sub">
    {{ filename }} &middot; recorded in logs/writes.jsonl
  </p>
</div>

<div class="stats">
  <div class="tile"><b style="color:var(--accent)">{{ succeeded }}</b><span>Wrote successfully</span></div>
  <div class="tile"><b style="color:var(--danger)">{{ failed|length }}</b><span>Failed</span></div>
  <div class="tile"><b style="color:var(--ink3)">{{ unchanged }}</b><span>Already correct, skipped</span></div>
  <div class="tile"><b style="color:var(--danger)">{{ unresolved|length }}</b><span>Unmatched, not written</span></div>
</div>

{% if failed %}
<div class="warnbar">
  {{ failed|length }} rows failed. Every write is idempotent per campaign-month,
  so re-uploading the same sheet will retry them safely.
</div>
<table>
  <tr><th>CAMPAIGN</th><th>PERIOD</th><th class="num">DAILY COST</th><th>ERROR</th></tr>
  {% for o in failed %}
  <tr>
    <td class="name">{{ o.plan.campaign_name }}</td>
    <td>{{ o.plan.year }}-{{ '%02d' % o.plan.month }}</td>
    <td class="num">{{ o.plan.conversion.daily_cost }}</td>
    <td class="err">{{ o.error }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

<div class="foot">
  {% if unresolved %}
  <a class="ghost" href="/unmatched/{{ session_id }}.csv">
    Download {{ unresolved|length }} unmatched rows
  </a>
  {% else %}<span class="note">Every row resolved.</span>{% endif %}
  <form method="get" action="/"><button class="primary" type="submit">Upload another sheet</button></form>
</div>
{% endblock %}
```

- [ ] **Step 4: Add the write route to app.py**

Add the import:

```python
from st_cost_uploader.writer import execute as execute_plan
```

Add the route at the end of the file:

```python
@app.post("/write/{session_id}", response_class=HTMLResponse)
async def write_costs(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    if not session.plans:
        raise HTTPException(
            status_code=400,
            detail="Build a preview before writing. Nothing has been sent to ServiceTitan.",
        )

    client = get_client(session.tenant)
    try:
        session.outcomes = await execute_plan(
            session.plans, client, AuditLog(AUDIT_PATH), session.tenant
        )
    finally:
        await client.aclose()

    failed = [o for o in session.outcomes if not o.ok]
    return TEMPLATES.TemplateResponse(
        request,
        "results.html",
        {
            "step": 3,
            "session_id": session.id,
            "tenant": session.tenant,
            "filename": session.filename,
            "attempted": len(session.outcomes),
            "succeeded": len(session.outcomes) - len(failed),
            "failed": failed,
            "unchanged": sum(1 for p in session.plans if p.action is Action.NO_CHANGE),
            "unresolved": session.unresolved,
        },
    )
```

- [ ] **Step 5: Run the full suite to verify everything passes**

```bash
uv run pytest -v
```

Expected: every test across all tasks passes.

- [ ] **Step 6: Update the README**

Add this section to `README.md`, replacing the "Status" section:

```markdown
## Running it

```bash
cp .env.example .env    # then fill in your ServiceTitan credentials
uv sync --extra dev
uv run uvicorn st_cost_uploader.web.app:app --port 8000
```

Open http://127.0.0.1:8000, pick a tenant, drop in a spreadsheet.

Nothing reaches ServiceTitan until you approve the preview. Every write is
recorded in `logs/writes.jsonl`.

## Tests

```bash
uv run pytest
```

No test touches the network. The ServiceTitan client is exercised against
`httpx.MockTransport` and the web layer against a stub client.
```

- [ ] **Step 7: Update next_steps.md**

Replace the "Immediate next action" section with the current state: the build is complete, what has been verified live, and what has only been exercised against fixtures. Keep the "Verification owed" section for anything Task 1 could not settle.

- [ ] **Step 8: Manual end-to-end check against a real tenant**

**This writes to production. Get the operator's approval before running it.**

Build a small spreadsheet with two or three campaign-months that currently read `0.0`, run the app, walk all four screens, and confirm:

1. The preview daily costs match hand calculation.
2. The written values appear in ServiceTitan when read back through the MCP server.
3. `logs/writes.jsonl` has one line per write.
4. Re-uploading the same sheet produces `NO CHANGE` rather than a second write.

Then restore the original values.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: add write execution and results screen, complete the pipeline"
```

---

## Self-Review

**Spec coverage.** Every numbered component in the spec maps to a task: parser (4), resolver (6), alias store (5), calculator (3), ServiceTitan client (8, 9), planner (10), preview gate (15), writer (12), audit log (11). Auth and configuration land in Task 7. The behavior table's six rows are each covered by a named test. Out-of-scope items appear nowhere, which is correct.

**Known deviation.** The spec describes three screens; this plan builds four. The results screen is required by the spec's own error handling section, which states that per-row write failures are recorded and reported. There is nowhere to report them without it. It is not mocked in the Pencil file.

**Ordering note.** Task 14 leaves `/confirm` calling a `preview_screen` stub, which Task 15 replaces. This is deliberate: splitting the resolve and preview screens keeps each task independently reviewable, and Task 14's tests assert on session state and the alias file rather than on the stub's response body.

**Type consistency.** `PeriodKey` is `tuple[int, int, int]` and is produced by `CostRecord.period_key`, `PlannedWrite.period_key`, and the keys of `list_costs_for_campaigns`. `daily_cost` is `Decimal` at every boundary except `_cost_body`, where a single documented `float()` call serializes it for JSON transport. `Action` and `MatchKind` are `str` enums so templates can compare `.value` directly.
