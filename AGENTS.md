# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, Cursor, Copilot, and others) when working with code in this repository.

## What this is

ServiceTitan's built-in interface for entering marketing campaign costs takes one value per campaign per month and is slow and error-prone at any real volume. This tool lets a marketer upload campaign costs from a spreadsheet instead, mapping human-friendly rows (campaign name, month, total spend) onto what the ServiceTitan Marketing v2 API wants.

**Status (2026-08-13):** build complete, reviewed, and verified end to end against the live `acme_east` tenant. Nothing is queued. The items left in `next_steps.md` are hardening: a large-sheet run, and a duplicate-create question the UI can no longer reach.

## Session start

1. Run `git status --short --branch` and `git remote -v`, then `git pull --ff-only`. Origin is `jtron9k/st-campaign-cost-uploader`.
2. Read `next_steps.md`. It is the current handoff. Verify its claims against the repository and update it before ending a session with material work outstanding.

## Commands

Python 3.12+, managed with `uv`. The web layer is FastAPI with Jinja2 templates.

- Setup: `cp .env.example .env` and fill in credentials, then `uv sync --extra dev`
- Run: `uv run uvicorn st_cost_uploader.web.app:app --port 8000`, then open http://127.0.0.1:8000
- Test: `uv run pytest`. About 160 tests, none touching the network: the client runs against `httpx.MockTransport` and the web layer against a stub client.
- Lint: `uv run ruff check`
- Live read-only probe: `uv run python scripts/st_probe.py tenants`, or `... campaigns acme_east --match google`. It calls only GET methods and never imports `create_cost` or `update_cost`; `tests/test_probe_is_read_only.py` enforces that.

## Layout

`src/st_cost_uploader/`:

- `parser.py`: spreadsheet reading and layout normalization
- `normalize.py`: shared campaign-name normalization used by the resolver and the alias store
- `resolver.py`: spreadsheet campaign names to ServiceTitan campaign IDs (exact, alias, then fuzzy via `rapidfuzz`)
- `aliases.py`: per-tenant map from spreadsheet name to campaign ID, persisted in `aliases/<tenant>.json` (committed on purpose; it holds no secrets and the team shares it)
- `money.py`: the money path, `Decimal` only, never float
- `planner.py`: per resolved row, decide create, update, or no change
- `writer.py`: executes an approved plan behind a `MAX_CONCURRENCY = 8` semaphore
- `audit.py`: append-only record of every attempted write, in `logs/writes.jsonl` (gitignored)
- `client.py`: async Marketing v2 client
- `config.py`: credential loading, with the same variable naming as `../service_titan_mcp`
- `models.py`: frozen domain types
- `samples.py`: the sample spreadsheets offered from the upload screen
- `web/app.py`: FastAPI layer that holds session state and renders, deferring every decision to the modules above

`docs/superpowers/specs/` and `docs/superpowers/plans/` hold the 2026-08-12 design and plan plus the 2026-08-13 sample-spreadsheet addition.

## Config and secrets

`.env` holds `ST_TENANTS` plus, per tenant slug, `ST_TENANT_<SLUG>_ID`, `_CLIENT_ID`, `_CLIENT_SECRET`, and `_APP_KEY`. Every listed slug needs a complete block, or startup fails naming the missing variable. Never commit `.env`. `data/`, `private/`, and `logs/` are gitignored because they hold real customer spend and the write audit log.

## Domain facts (verified against the live API)

Confirmed by querying tenant `acme_east`, first through the `servicetitan-local` MCP server and later through `scripts/st_probe.py`. They drive most design decisions, so verify before contradicting them, and record anything newly verified here with the date so later sessions do not re-derive it.

### The cost record shape

`GET /marketing/v2/tenant/{tenant_id}/costs` returns:

```json
{ "id": 500000001, "year": 2022, "month": 1, "dailyCost": 0.0, "campaignId": 1000001 }
```

One record is scoped to a single `(campaignId, year, month)` triple. There is no date-range or weekly granularity. `GET .../costs/{id}` returns a single record (verified 2026-08-12).

### `dailyCost` is the central trap

The stored field is a **daily** cost, but the record covers a **month**. Marketers work in monthly totals, so the tool owns this conversion:

```
dailyCost = monthlyTotal / daysInMonth(year, month)
```

- **Days in month varies**, so the divisor is per row, never a constant. February 2024 has 29 days; February 2026 has 28.
- **`dailyCost` stores to the cent** (verified 2026-08-12). Across 500 live records there were 68 distinct values: 433 whole numbers, 5 at one decimal place, 62 at two, and none above two.
- **Round-tripping therefore loses money.** $5,000 across February 2026 is $178.571428.../day; rounded to cents and multiplied back out, it reconstructs as $4,999.96. The residual is unavoidable and bounded at half a cent per day, so at most about **$0.155 per campaign-month**. The money path computes it explicitly in `Decimal` rather than letting a float cast decide it.
- ServiceTitan's own help documentation states the same formula: total campaign cost divided by the number of days the campaign runs.

### Writes are an upsert

ServiceTitan does **not** pre-create cost rows for every campaign, so for every `(campaign, year, month)` the tool looks up an existing record and updates it, or creates one when absent. Blind creates risk duplicate or rejected rows. Evidence and verified behavior:

- Campaign `1000001` has a record for **every** month from 2022-01 through 2026-12, 60 in all (verified 2026-08-13). Most are `0.0`; 2023-08 through 2023-10, 2024-01 through 2024-03, and all of 2025 carry real values. Every month it could be pointed at already has a record, so it can never exercise the CREATE path.
- Campaign `1000002` ("Google") had **zero** cost records before testing. Supervised creates on 2026-08-13, with Justin's approval, added 2026-12 and 2026-11; both were then set back to `0.0`. **They cannot be removed.** Neither the Marketing v2 API nor `client.py` exposes a delete for cost records, so a further CREATE test against this campaign needs a third month.
- **`POST /costs`** (verified 2026-08-13) takes the full body `{campaignId, year, month, dailyCost}` and mints a real record with a new id. **Its response body carries no id.** `logs/writes.jsonl` logged `"cost_id": null` while the read-back showed the record created as `500000004`. To learn a created record's id, re-read the campaign's costs and match on `(year, month)`; never build a follow-up request from `create_cost`'s return value.
- **`PATCH /costs/{id}`** (verified 2026-08-12 on record `500000001`, restored to `0.0` afterward) accepts a partial body of just `{"dailyCost": <number>}`, returns HTTP 200 with `{"id": ...}` only, and updates in place with the same id and unchanged `year`, `month`, and `campaignId`. `{"dailyCost": 1.23}` read back as `1.23` and `{"dailyCost": 0}` as `0.0`. Send only `dailyCost` on update; resending the identifying fields is untested and carries risk for no benefit.
- Record ids arrive in three contiguous blocks (`500000001`+ for 2022 to 2024, `500000002`+ for 2025, `500000003`+ for 2026), which suggests ServiceTitan bulk-creates a year of rows at a time. Treat that as an observation, not a guarantee.
- Whether a second create for an existing `(campaignId, year, month)` is rejected or duplicated remains unknown. The planner looks up first, and `POST /write` is single-shot (a second submit returns HTTP 409), so the UI cannot reach the case.

### Campaigns and matching

`GET /marketing/v2/tenant/{tenant_id}/campaigns` returns `id`, `name`, `active`, `isDefaultCampaign`, a `category` object, plus `source` and `medium` fields that are frequently `null`.

Spreadsheets carry campaign **names** and the API needs campaign **IDs**, so name resolution is a core problem. Names are neither unique nor stable, and many carry category strings like `Search||Google||NoCost`. Measured on `acme_east` on 2026-08-13: **2,489 campaigns, 2,470 distinct names after normalization, so 19 names are shared by more than one campaign.** The resolver's refusal to auto-resolve a name matching two campaigns is load-bearing, and the operator resolves ambiguous and unmatched names as a normal case.

### Tenants

Four tenants are configured (`.env.example` lists them): `acme_east`, `acme_west`, `northwind`, `globex`. Treat multi-tenant as a requirement.

## Working with the ServiceTitan API

- The `servicetitan-local` and `servicetitan-multi` MCP servers, where configured, and `scripts/st_probe.py` are for **read-only exploration** of live data. Use them to check real shapes instead of guessing.
- Every tenant is **production with real business data**. Never issue a POST, PATCH, or PUT outside the app's preview gate, including through the `servicetitan_api_call` escape hatch, without explicit per-instance approval from Justin. Reads are fine.
- The main API allows roughly 60 req/sec (the MCP client self-limits near 30). Reporting endpoints are capped near 5 req/min, so avoid them.
- Trust the `hasMore=` footer over the returned item count when paging.
