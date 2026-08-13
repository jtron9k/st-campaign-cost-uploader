# Sample Spreadsheet Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer two downloadable CSV samples from the upload screen, one per supported spreadsheet layout, so an operator's first upload is informed rather than a guess.

**Architecture:** A new engine module `samples.py` holds one sample dataset and renders it to CSV in either layout. A thin FastAPI route serves it. Both layouts render from the same dataset, so a test can parse each through the app's own `parse_workbook` and assert they produce identical rows, which is what stops the samples drifting from the parser or from each other.

**Tech Stack:** Python 3.12, stdlib `csv` and `calendar`, FastAPI `StreamingResponse`, Jinja2 template, pytest.

**Spec:** `docs/superpowers/specs/2026-08-13-sample-spreadsheet-download-design.md`

## Global Constraints

- CSV only. No `.xlsx` samples are produced.
- No static-file mount is introduced. The CSV is generated in memory, matching the existing `GET /unmatched/{session_id}.csv` route.
- No JavaScript in the template. Plain anchors only.
- The sample's months are computed from today's date, never hardcoded.
- The route takes no session, no tenant, and makes no network call.
- `samples.py` lives in the engine package and must not import anything from `st_cost_uploader.web`.
- Money is `Decimal` in tests, never float.
- `ruff` line-length is 100. Run `uv run ruff check` before each commit.
- Header labels in the long sample are exactly `Campaign`, `Month`, `Total Spend`.
- Filenames served are `campaign-costs-sample-long.csv` and `campaign-costs-sample-wide.csv`.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/st_cost_uploader/samples.py` (create) | The sample dataset, month arithmetic, and the two CSV renderers. Pure, no web dependency. |
| `tests/test_samples.py` (create) | Round-trips each sample through `parse_workbook` and asserts the two layouts agree. |
| `src/st_cost_uploader/web/app.py` (modify) | One route, `GET /sample/{layout}.csv`. |
| `src/st_cost_uploader/web/templates/upload.html` (modify) | Two anchors below the drop zone. |
| `tests/test_web.py` (modify) | Route status, content type, filename, 404, and the template links. |

---

### Task 1: The sample dataset and its two renderers

**Files:**
- Create: `src/st_cost_uploader/samples.py`
- Test: `tests/test_samples.py`

**Interfaces:**
- Consumes: `st_cost_uploader.parser.parse_workbook` (tests only).
- Produces:
  - `SAMPLE_LAYOUTS: tuple[str, ...]` — `("long", "wide")`
  - `sample_months(today: date | None = None) -> list[tuple[int, int]]` — three `(year, month)` pairs
  - `sample_csv(layout: str, today: date | None = None) -> str` — the CSV text; raises `ValueError` on an unknown layout

- [ ] **Step 1: Write the failing tests**

Create `tests/test_samples.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from st_cost_uploader.parser import parse_workbook
from st_cost_uploader.samples import SAMPLE_LAYOUTS, sample_csv, sample_months

# November crosses a year boundary, so the month arithmetic is exercised
# rather than assumed: Nov 2026, Dec 2026, Jan 2027.
FIXED = date(2026, 11, 15)


def _parsed(layout: str, today: date = FIXED):
    return parse_workbook(sample_csv(layout, today).encode("utf-8"), f"sample-{layout}.csv")


def _spend(result) -> set[tuple[str, int, int, Decimal]]:
    return {(r.campaign_name, r.year, r.month, r.monthly_total) for r in result.rows}


def test_months_start_at_this_month_and_roll_over_the_year():
    assert sample_months(FIXED) == [(2026, 11), (2026, 12), (2027, 1)]


def test_long_sample_is_detected_as_long_and_parses_clean():
    result = _parsed("long")

    assert result.layout == "long"
    assert len(result.rows) == 7
    assert result.errors == ()


def test_wide_sample_is_detected_as_wide_and_parses_clean():
    result = _parsed("wide")

    assert result.layout == "wide"
    assert len(result.rows) == 7
    assert result.errors == ()


def test_both_layouts_describe_the_same_spend():
    """The load-bearing test. The two files are one dataset rendered twice,
    so if they ever disagree the samples are lying to the operator."""
    assert _spend(_parsed("long")) == _spend(_parsed("wide"))


def test_a_blank_cell_is_skipped_rather_than_read_as_zero():
    rows = [r for r in _parsed("wide").rows if r.campaign_name == "Direct Mail - North"]

    assert len(rows) == 1
    assert rows[0].monthly_total == Decimal("1500.00")


def test_long_headers_are_the_ones_the_detector_matches_on():
    assert _parsed("long").column_mapping == {
        "Campaign": "campaign_name",
        "Month": "period",
        "Total Spend": "monthly_total",
    }


def test_every_declared_layout_renders():
    for layout in SAMPLE_LAYOUTS:
        assert sample_csv(layout, FIXED).strip()


def test_an_unknown_layout_is_rejected():
    with pytest.raises(ValueError, match="sideways"):
        sample_csv("sideways", FIXED)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_samples.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'st_cost_uploader.samples'`

- [ ] **Step 3: Write the implementation**

Create `src/st_cost_uploader/samples.py`:

```python
"""The sample spreadsheets offered from the upload screen.

Both layouts render from one dataset, so the two files cannot describe
different spend, and a parser change that breaks either one breaks the
tests. A sample that no longer parses is worse than no sample at all.
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import date

SAMPLE_LAYOUTS = ("long", "wide")
SAMPLE_MONTH_COUNT = 3

# (campaign name, month offset from the first sample month, amount).
#
# "Direct Mail - North" is funded in one month only. That leaves blank cells
# in the wide file, which is how the sample documents that an empty cell is
# skipped rather than read as 0 -- an operator whose campaign ran for part of
# a year needs to know that, and a fully populated sample would not show it.
_SAMPLE_SPEND: tuple[tuple[str, int, str], ...] = (
    ("Spring Radio Push", 0, "4000.00"),
    ("Spring Radio Push", 1, "4000.00"),
    ("Spring Radio Push", 2, "3000.00"),
    ("Direct Mail - North", 1, "1500.00"),
    ("Google Search Brand", 0, "8250.00"),
    ("Google Search Brand", 1, "8250.00"),
    ("Google Search Brand", 2, "9000.00"),
)


def _month_at(start: date, offset: int) -> tuple[int, int]:
    index = start.month - 1 + offset
    return start.year + index // 12, index % 12 + 1


def sample_months(today: date | None = None) -> list[tuple[int, int]]:
    """This month and the two after it.

    Computed rather than hardcoded on purpose. A sample frozen at a past
    month invites an operator to fill in amounts without touching the month
    column and write real spend to the wrong period. The preview gate would
    catch that, but the sample should not set the trap.
    """
    start = today or date.today()
    return [_month_at(start, offset) for offset in range(SAMPLE_MONTH_COUNT)]


def _long_rows(months: list[tuple[int, int]]) -> list[list[str]]:
    # These three headers are exactly what the parser's column patterns
    # match on, so the sample states what the detector wants rather than
    # approximating it.
    rows = [["Campaign", "Month", "Total Spend"]]
    for name, offset, amount in _SAMPLE_SPEND:
        year, month = months[offset]
        rows.append([name, f"{year}-{month:02d}", f"${amount}"])
    return rows


def _wide_rows(months: list[tuple[int, int]]) -> list[list[str]]:
    # The column headers spell the period as "Nov 2026" while the long file
    # spells it "2026-11". Both parse. Showing two spellings tells the
    # operator the parser is tolerant, so they do not reformat a sheet that
    # already works.
    rows = [["Campaign"] + [f"{calendar.month_abbr[m]} {y}" for y, m in months]]

    by_campaign: dict[str, dict[int, str]] = {}
    for name, offset, amount in _SAMPLE_SPEND:
        by_campaign.setdefault(name, {})[offset] = amount

    for name, amounts in by_campaign.items():
        rows.append(
            [name]
            + [f"${amounts[i]}" if i in amounts else "" for i in range(len(months))]
        )
    return rows


def sample_csv(layout: str, today: date | None = None) -> str:
    """Render the sample dataset as CSV text in the requested layout."""
    if layout not in SAMPLE_LAYOUTS:
        raise ValueError(
            f"Unknown sample layout {layout!r}. Use one of {', '.join(SAMPLE_LAYOUTS)}."
        )

    months = sample_months(today)
    rows = _long_rows(months) if layout == "long" else _wide_rows(months)

    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_samples.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check`
Expected: 195 passed, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/st_cost_uploader/samples.py tests/test_samples.py
git commit -m "feat: render the sample spreadsheet in both supported layouts

One dataset, two renderers. The tests round-trip each layout through
parse_workbook and assert the two produce identical rows, so the samples
cannot drift from the parser or from each other.

Months are computed from today rather than hardcoded, so a frozen sample
never invites an operator to fill in amounts and write to a past period."
```

---

### Task 2: Serve the samples and link them from the upload screen

**Files:**
- Modify: `src/st_cost_uploader/web/app.py` (add an import, and a route after `unmatched_csv`)
- Modify: `src/st_cost_uploader/web/templates/upload.html:16-20`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `sample_csv`, `SAMPLE_LAYOUTS` from Task 1.
- Produces: `GET /sample/{layout}.csv`, returning `200` with `text/csv` for a known layout and `404` otherwise.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
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

    assert '/sample/long.csv' in body
    assert '/sample/wide.csv' in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -k sample -v`
Expected: FAIL with `404` on the two download tests (the route does not exist yet), and the link test failing on the missing hrefs.

- [ ] **Step 3: Add the route**

In `src/st_cost_uploader/web/app.py`, add to the imports:

```python
from st_cost_uploader.samples import sample_csv
```

Then add this route immediately after `unmatched_csv`:

```python
@app.get("/sample/{layout}.csv")
def sample_spreadsheet(layout: str) -> StreamingResponse:
    """A format example, one per supported layout.

    No session, no tenant, no network call: an operator reaches this before
    they have chosen anything, which is exactly when they need it.
    """
    try:
        text = sample_csv(layout)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return StreamingResponse(
        io_module.StringIO(text),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="campaign-costs-sample-{layout}.csv"'
            )
        },
    )
```

- [ ] **Step 4: Add the links to the template**

In `src/st_cost_uploader/web/templates/upload.html`, replace the `drop` div (lines 16-20) with:

```html
  <div class="drop" style="margin-top:24px">
    <b>Choose a spreadsheet</b>
    <span>.xlsx or .csv &middot; long or wide layout &middot; up to about 1,000 rows</span>
    <p><input type="file" name="file" accept=".xlsx,.csv" required></p>
  </div>
  <p class="note" style="margin-top:8px">
    Not sure of the format? Download a sample:
    <a href="/sample/long.csv">long</a> &middot;
    <a href="/sample/wide.csv">wide</a>
  </p>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py -k sample -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check`
Expected: 200 passed, `All checks passed!`

- [ ] **Step 7: Verify it in the browser**

Start the app and confirm the links render and download:

```bash
uv run uvicorn st_cost_uploader.web.app:app --port 8000
```

Open `http://127.0.0.1:8000`, confirm the sample line appears below the drop zone, download both files, and re-upload each one. The long file should be detected as LONG LAYOUT with 7 rows and 0 skipped; the wide file as WIDE LAYOUT with 7 rows and 0 skipped. Stop at the resolve screen; do not write.

- [ ] **Step 8: Commit**

```bash
git add src/st_cost_uploader/web/app.py src/st_cost_uploader/web/templates/upload.html tests/test_web.py
git commit -m "feat: offer both sample layouts from the upload screen

GET /sample/{layout}.csv serves the format examples, and the upload screen
links them below the drop zone. The route takes no session, tenant, or
network call, so it works before the operator has chosen anything and
survives a broken tenant config."
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
| --- | --- |
| `GET /sample/{layout}.csv`, 404 on unknown | Task 2, Steps 1 and 3 |
| `StreamingResponse`, `text/csv`, attachment filename | Task 2, Steps 1 and 3 |
| No session, tenant, or network | Task 2, Step 1 (`test_the_sample_route_needs_no_tenant_configured`) |
| Mirrors `/unmatched/{id}.csv`, no static mount | Task 2, Step 3 |
| One dataset renders both layouts | Task 1, Step 3 (`_SAMPLE_SPEND`) |
| `Direct Mail - North` funded once, blanks in wide | Task 1, Steps 1 and 3 |
| Headers are the parser's match patterns | Task 1, Step 1 (`test_long_headers_are_the_ones_the_detector_matches_on`) |
| Two period spellings, `2026-11` and `Nov 2026` | Task 1, Step 3 (`_long_rows`, `_wide_rows`) |
| Months computed from today | Task 1, Steps 1 and 3 (`sample_months`) |
| Upload-screen link line, no JavaScript | Task 2, Step 4 |
| All six spec tests | Task 1 Step 1 (4 of them), Task 2 Step 1 (2 of them) |

No spec requirement is unimplemented.

**Placeholder scan:** No TBD, TODO, "handle edge cases", or "similar to Task N". Every code step carries the actual code.

**Type consistency:** `sample_csv(layout, today)` and `sample_months(today)` keep the same signatures in Task 1's implementation, Task 1's tests, and Task 2's route. `SAMPLE_LAYOUTS` is a tuple in both its definition and its use. The route's `layout` path parameter is `str`, matching `sample_csv`'s first parameter.

**Note on test counts:** the expected totals (195 after Task 1, 200 after Task 2) assume the suite stands at 187, which is where `main` is as of commit `03455f6`. Adjust if other work lands first.
