# ST Campaign Cost Uploader: design

**Date:** 2026-08-12
**Status:** Approved. Ready for an implementation plan.
**Visual reference:** `st_cost_updater.pen` (three screens, left to right)

## Purpose

ServiceTitan's campaign cost interface accepts one value per campaign per month, typed in by hand. The field it stores is a daily cost even though the record covers a month, so a marketer holding a monthly ad-spend number has to divide by the length of that month before entering it. At any real campaign count this is slow and easy to get wrong.

This tool reads a spreadsheet of campaign spend and writes it to ServiceTitan through the Marketing v2 API. It owns the monthly-to-daily conversion, the campaign name resolution, and the create-versus-update decision.

## Users and delivery

A local web app, run by the marketing team. Each operator runs it on their own machine.

**Stack:** Python with FastAPI, server-rendered HTML with htmx. No build step, one command to start.

Python was chosen for one reason above the others: `decimal` is a language builtin. Silent binary-float error in the money path is the specific failure this tool has to avoid, and a native decimal type removes the chance of forgetting to route through a library. `openpyxl` covers both spreadsheet layouts, and the existing `service_titan_mcp` gives a working reference for tenant credentials and OAuth.

The engine is a plain Python package with no web dependency. FastAPI is a thin layer over it, which leaves a CLI or a scheduled runner possible later without restructuring.

## Verified API behavior

The facts in `CLAUDE.md` govern. Confirmed on 2026-08-12 against one live tenant:

- A cost record is `{id, year, month, dailyCost, campaignId}`, scoped to one `(campaignId, year, month)` triple. No date-range or weekly granularity exists.
- `dailyCost` stores to the cent. Across 500 live records there were 68 distinct values and none carried more than two decimal places.
- `GET /marketing/v2/tenant/{tenant_id}/costs/{id}` returns a single record, so the single-cost resource path exists.
- ServiceTitan's own help documentation states the conversion as total campaign cost divided by the number of days the campaign runs, matching the derivation in `CLAUDE.md`.

One item stays unresolved: the update verb. The developer portal is an authenticated single-page app and cannot be read without a session. Convention across ServiceTitan's v2 API and the confirmed `/costs/{id}` resource path both point to `PATCH /marketing/v2/tenant/{tenant_id}/costs/{id}`. Confirming it requires a real write, so it becomes the first task in the implementation plan, run against a campaign-month currently reading `0.0`, with the operator's approval, writing a known value and re-reading to confirm.

## Architecture

```
spreadsheet -> parser -> resolver -> calculator -> planner -> PREVIEW GATE -> writer -> audit log
                            ^                                      ^
                       alias store                        ServiceTitan client
```

Nine components, each with one purpose and a defined interface.

### 1. Parser

Reads `.xlsx` and `.csv`. Detects long layout (one row per campaign-month) or wide layout (campaigns as rows, months as columns) from the header row, and normalizes both into one canonical row type: `(campaign_name, year, month, monthly_total)`.

The detected layout and the resulting column mapping appear in the UI with a manual override. Auto-detection can misread a file, and the preview gate exists anyway, so the design surfaces the interpretation instead of hiding it.

Malformed rows are collected as errors and returned alongside the good ones. The parser raises only when a file yields zero usable rows.

### 2. Resolver

Three tiers, in order:

1. Exact match against the tenant's campaign names, after normalizing case and collapsing whitespace.
2. The persisted alias map.
3. Ranked fuzzy candidates, scored with `rapidfuzz` token-set ratio. The top five candidates above a score of 60 are offered. Below that the row is treated as unmatched.

An alias always beats a higher-scoring fuzzy candidate, because the alias records a human decision and the score does not.

Anything unresolved by tiers one and two goes to a resolution screen where the operator picks a campaign or skips the row. Every confirmed pick writes to the alias store.

### 3. Alias store

One JSON file per tenant at `aliases/<tenant>.json`, mapping spreadsheet name to campaign ID. Committed to git so the team shares the accumulated mappings. It holds no secrets.

### 4. Calculator

`Decimal` throughout, never `float`.

```
daily_cost = quantize(monthly_total / days_in_month(year, month), 2, ROUND_HALF_UP)
reconstructed = daily_cost * days_in_month(year, month)
residual = reconstructed - monthly_total
```

`days_in_month` comes from `calendar.monthrange`, so leap years are handled by the standard library rather than by hand. The divisor is computed per row. February 2024 divides by 29, February 2026 by 28.

Because ServiceTitan stores only cents, the residual cannot be eliminated. It is bounded at half a cent per day, so at most about $0.155 per campaign-month. The calculator returns it, and the preview displays it as its own column rather than a footnote, since a marketer reconciling against an ad platform will look for that number specifically.

### 5. ServiceTitan client

OAuth client-credentials auth with a cached, refreshed token. Reads campaigns. Reads existing costs filtered by `campaign_id` for the distinct campaigns present in the sheet. Creates and updates cost records.

Filtering per campaign rather than scanning the tenant's full cost history makes the read cost scale with sheet size instead of tenant age.

### 6. Planner

For each resolved row, looks up `(campaignId, year, month)` among the fetched costs and emits `CREATE`, `UPDATE` carrying the existing value, or `NO_CHANGE`.

This is the upsert decision, isolated so it can be unit-tested against fixtures. ServiceTitan does not pre-create cost rows for every campaign, so a blind create risks duplicate or rejected rows.

### 7. Preview gate

Nothing writes without passing here.

Columns: campaign name as written, resolved campaign, period, monthly total, days in month, computed `dailyCost`, reconstructed total, residual, prior value, action.

Rows that replace an existing non-zero cost are tinted and show what they are replacing. Unmatched rows are listed separately with a download. Summary tiles carry the write count, the overwrite count, the skipped count, and the net residual.

The operator approves the batch once. Per-row confirmation was considered and rejected as unusable on a large correction.

### 8. Writer

Executes the approved plan with at most 8 concurrent requests. ServiceTitan allows roughly 60 per second and the existing MCP client self-limits near 30, so 8 leaves headroom without making a 1,000-row upload slow.

Per-row failures are recorded and the run continues. Every write is idempotent per campaign-month, so re-running a failed subset cannot double-count.

### 9. Audit log

Append-only JSONL under `logs/`. One line per write: timestamp, tenant, campaign ID and name, year, month, prior value, new value, result.

These are production tenants and a written record of what changed is cheap to produce.

## Auth and configuration

A single set of ServiceTitan app credentials in a gitignored `.env`, with a tenant ID per configured tenant. The operator selects the tenant in the UI at upload time.

This mirrors the `servicetitan-local` MCP server's existing configuration shape. Credentials never enter the browser and never enter git.

## Behavior decisions

| Situation | Behavior |
| --- | --- |
| Spreadsheet layout ambiguous | Detect, display the interpretation, allow manual override |
| Campaign name not an exact match | Alias map, then ranked fuzzy candidates for the operator |
| Campaign name unresolvable | Write the matched rows, report the unmatched ones for a follow-up run |
| Target already holds a non-zero cost | Overwrite, tinted and showing the prior value in the preview |
| Rounding residual | Always computed, always displayed as its own column |
| Write fails mid-batch | Record it, continue, report it; re-running the failed rows is safe |

## Error handling

Parse failures and unresolved names are normal states with dedicated UI, not exceptions. Authentication failure surfaces at tenant selection rather than partway through a write. API errors during the write phase are per-row and non-fatal. The only hard stop is a file that produces no usable rows.

## Testing

Unit coverage concentrates where being wrong costs money:

- `days_in_month` across leap and non-leap February.
- The rounding table, including the cases in the preview mockup: $5,000 over February 2026 yielding $178.57 and reconstructing to $4,999.96; $7,000 over February 2024 yielding $241.38 and reconstructing to $7,000.02; $1,860 over March yielding exactly $60.00 with zero residual.
- Both spreadsheet layouts, including a file that could plausibly parse either way.
- Resolver tier precedence, particularly that an alias beats a higher-scoring fuzzy candidate.
- Planner output for create, update, and no-change against fixture cost data.

The ServiceTitan client is tested against recorded response fixtures. The only live call in the test path is the one-time verb confirmation described above.

## Out of scope

Campaign creation, cost deletion, hosted deployment, multi-user authentication, and scheduled or automated uploads. Every write this tool makes sets a cost value on a campaign that already exists.

## Assumptions

Spreadsheets carry monthly totals rather than daily costs. Realistic uploads stay under roughly 1,000 rows. Both are stated so a later session can challenge them rather than inherit them silently.
