# Sample spreadsheet download: design

**Date:** 2026-08-13
**Status:** Approved. Ready for an implementation plan.
**Depends on:** `2026-08-12-campaign-cost-uploader-design.md` (parser, upload screen)

## Purpose

The upload screen tells an operator it accepts ".xlsx or .csv, long or wide layout" and leaves them to work out what those mean. Nothing on screen shows the column names the parser looks for, and the wide layout has no user-facing documentation at all. An operator's first upload is therefore a guess, and a wrong guess costs a round trip through a parse error.

A downloadable sample answers the question in the place it gets asked.

## Scope

Two CSV files, offered from the upload screen, showing the same three campaigns of spend in each of the two supported layouts.

**Explicitly not in scope:**

- Pre-filling the sample with the tenant's real campaign names. Considered and rejected: it makes the sample a working starting point rather than a format example, needs a tenant selected and a live API call before the file can be built, and adds a second thing to keep working. The format example is what closes the gap described above.
- `.xlsx` samples. The parser reads CSV and XLSX into the same `SheetRow` list, so a CSV sample teaches the XLSX path too, and CSV keeps a binary artifact out of the repo.

## Behaviour

### Route

`GET /sample/{layout}.csv`

- `layout` is `long` or `wide`. Any other value returns **404**.
- Returns a `StreamingResponse`, `media_type="text/csv"`, with
  `Content-Disposition: attachment; filename="campaign-costs-sample-{layout}.csv"`.
- Takes no session, no tenant, and makes no network call. It is reachable before an operator has chosen anything.

This mirrors the existing `GET /unmatched/{session_id}.csv`, which already establishes in-memory CSV generation as the pattern in this app. No static-file mount is introduced.

### Content

One dataset renders to both files, so the two samples describe identical spend.

Long:

```
Campaign,Month,Total Spend
Spring Radio Push,2026-01,$4000.00
Spring Radio Push,2026-02,$4000.00
Spring Radio Push,2026-03,$3000.00
Direct Mail - North,2026-02,$1500.00
Google Search Brand,2026-01,$8250.00
Google Search Brand,2026-02,$8250.00
Google Search Brand,2026-03,$9000.00
```

Wide:

```
Campaign,Jan 2026,Feb 2026,Mar 2026
Spring Radio Push,$4000.00,$4000.00,$3000.00
Direct Mail - North,,$1500.00,
Google Search Brand,$8250.00,$8250.00,$9000.00
```

Three decisions inside that content:

- **`Direct Mail - North` carries one month only.** In the wide file that leaves two blank cells, which demonstrates that a blank is skipped rather than read as `0`. An operator with a campaign that ran for part of a year needs to know this, and a sample where every cell is populated would not tell them.
- **The header labels are the ones the parser matches on.** `Campaign`, `Month`, and `Total Spend` hit `_CAMPAIGN_PATTERN`, `_PERIOD_PATTERN`, and `_AMOUNT_PATTERN` respectively. The sample is therefore an accurate statement of what the detector wants, not an approximation.
- **Amounts carry `$` and the long file uses `2026-01` while the wide file uses `Jan 2026`.** Both are formats `parse_money` and `parse_period` already accept. Showing two period spellings tells the operator the parser is tolerant, which reduces the chance they reformat a working sheet unnecessarily.

### Months are computed, not hardcoded

The three months run from the current month forward. A sample frozen at 2026-01 invites an operator to fill in amounts without touching the month column and write real spend to the wrong period. The preview gate would catch that before anything reached ServiceTitan, but a sample should not set the trap in the first place.

The literal months above are therefore illustrative of shape. The implementation derives them from today's date, and the tests assert the relationship (three consecutive months starting this month) rather than fixed values.

### Upload screen

One line below the drop zone, beside the existing layout hint:

> Not sure of the format? Download a sample: **long** · **wide**

Plain anchors to the two routes. No JavaScript.

## Testing

The sample's whole value is that it parses, so the tests run it through the real parser rather than asserting on strings.

| Test | Asserts |
| --- | --- |
| Long sample round-trips | `parse_workbook` detects `long`, returns 7 rows, 0 errors |
| Wide sample round-trips | `parse_workbook` detects `wide`, returns 7 rows, 0 errors |
| The two samples agree | Both parse to an identical set of `(campaign_name, year, month, monthly_total)` |
| Blank cells are skipped, not zeroed | The wide sample yields no row for `Direct Mail - North` outside its one funded month |
| Route serves each layout | 200, `text/csv`, `Content-Disposition` names the file |
| Unknown layout | `GET /sample/sideways.csv` returns 404 |

The third test is the one that earns its place. It guarantees the two files cannot drift apart, and that neither can drift from what the parser accepts, which is the failure mode that would make the feature worse than useless.

## Risks

- **A stale sample is worse than no sample.** Addressed by generating from a single dataset and testing through the real parser, so a parser change that breaks the sample breaks the build.
- **An operator uploads the sample unedited.** They would write three fabricated campaigns that almost certainly do not resolve, land on screen 2 with three unmatched rows, and write nothing. The preview gate makes this self-correcting.
