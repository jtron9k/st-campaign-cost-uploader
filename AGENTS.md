# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, Cursor, Copilot, and others) when working with code in this repository.

## Project status

Greenfield. No application code exists yet. The stack is undecided pending a written spec (see `next_steps.md`). Do not assume a language, framework, or build system; if you need one before the spec lands, ask.

## What this project is

ServiceTitan's built-in interface for entering marketing campaign costs is slow and error-prone at any real volume. This tool lets a marketer upload campaign costs from a spreadsheet instead, mapping human-friendly rows (campaign name, month, total spend) onto what the ServiceTitan API actually wants.

## Domain facts (verified against the live API on 2026-08-12)

These were confirmed by querying tenant `acme_east` through the `servicetitan-local` MCP server. They drive most design decisions, so verify before contradicting them.

### The cost record shape

`GET /marketing/v2/tenant/{tenant_id}/costs` returns:

```json
{ "id": 500000001, "year": 2022, "month": 1, "dailyCost": 0.0, "campaignId": 1000001 }
```

One record is scoped to a single `(campaignId, year, month)` triple. There is no date-range or weekly granularity.

### `dailyCost` is the central trap

The stored field is a **daily** cost, but the record covers a **month**. Marketers work in monthly totals, so the tool owns this conversion:

```
dailyCost = monthlyTotal / daysInMonth(year, month)
```

Consequences to design around:

- **Days-in-month varies**, so the divisor is per-row, never a constant. February 2024 has 29 days; February 2026 has 28.
- **Round-tripping loses money.** $5,000 across February 2026 is $178.571428…/day. Rounded to cents and multiplied back out, that reconstructs as $4,999.96. Decide explicitly how much precision to send and whether to surface the residual to the user. Do not let this get decided by accident inside a float cast.
- Use exact decimal arithmetic for the money path rather than binary floats.
- **`dailyCost` stores to the cent** (verified 2026-08-12). Across 500 live records in `acme_east` there were 68 distinct values: 433 whole numbers, 5 at one decimal place, 62 at two, and none above two. The residual is therefore unavoidable rather than a precision choice. It is bounded at half a cent per day, so at most about **$0.155 per campaign-month**.
- ServiceTitan's own help documentation states the same formula, as total campaign cost divided by the number of days the campaign runs.

### Writes are an upsert, not a create

ServiceTitan does **not** pre-create cost rows for every campaign. This is confirmed by counterexample within one tenant:

- Campaign `1000001` has a record for **every** month from 2022-01 through 2026-12, 60 in all, including all twelve months of 2026 sitting at `0.0` (verified 2026-08-13 via `scripts/st_probe.py`). Most are `0.0`, but 2023-08 through 2023-10, 2024-01 through 2024-03, and every month of 2025 carry real values. An earlier note here claimed all 60 were `0.0`; that was wrong.
- Campaign `1000002` ("Google") has **zero** cost records.

Consequence for testing: **campaign `1000001` cannot exercise the CREATE path.** Every month it could be pointed at already has a record, so the planner will choose UPDATE every time. Settling whether `POST /costs` behaves as assumed needs a campaign-month with no record &mdash; `1000002` is the known one.

**`POST /costs` is now verified (2026-08-13).** A supervised end-to-end run against `acme_east`, with Justin's approval, created a record for campaign `1000002` ("Google") at 2026-12 with `dailyCost` 80.65. Read back through `scripts/st_probe.py`, it returned as a real record with a newly minted id, `500000005`. So:

- Create genuinely mints a record where none existed; the upsert model is correct.
- The created record was afterwards set to `0.0` through the app. **It cannot be removed** &mdash; neither the Marketing v2 API nor `client.py` exposes a delete for cost records. Campaign `1000002` therefore now has exactly one cost record (2026-12 at `0.0`), where it previously had none. Any future test of the CREATE path against this campaign must pick a different month.
- **The POST response body carries no id (verified 2026-08-13).** A second supervised create, campaign `1000002` at 2026-11, logged `"cost_id": null` in `logs/writes.jsonl` while a follow-up read showed the record had really been created as `500000004`. The writer captures whatever `create_cost` returns, and `_request` parses any non-empty body, so a `null` here means the response genuinely does not name the record. This is the opposite of `PATCH`, whose response is `{"id": ...}`.
- **To learn a created record's id, re-read the campaign's costs and match on `(year, month)`.** Do not build a follow-up request path out of `create_cost`'s return value; it is always `0` in practice.
- Campaign `1000002` now holds two cost records, 2026-11 and 2026-12, both restored to `0.0`. A third CREATE test against it needs a third month.

The record ids also arrive in three contiguous blocks (`500000001`+ for 2022-2024, `500000002`+ for 2025, `500000003`+ for 2026), which suggests ServiceTitan bulk-creates a year of rows at a time. Do not depend on that; it is an observation, not a documented guarantee.

So for every `(campaign, year, month)` the tool must look up an existing record and update it, or create one when absent. Blind creates risk duplicate or rejected rows.

Both operations exist in the Marketing v2 API (`create` and `update` on costs). The create body is `{campaignId, year, month, dailyCost}`.

`GET /marketing/v2/tenant/{tenant_id}/costs/{id}` returns a single record (verified 2026-08-12).

The update verb is **`PATCH /marketing/v2/tenant/{tenant_id}/costs/{id}`** (verified 2026-08-12 against tenant `acme_east`, cost record `500000001`, with Justin's approval; the record was restored to its prior `0.0` afterward).

What the supervised write established:

- **Body:** a partial body of just `{"dailyCost": <number>}` is accepted. `campaignId`, `year`, and `month` do **not** need to be resent.
- **Response:** HTTP 200 with `{"id": 500000001}`. The body carries the id only, not the updated record.
- **It updates in place.** The re-read returned the same `id`, and `year`, `month`, and `campaignId` were unchanged. No new row was created.
- **Round trip:** `{"dailyCost": 1.23}` read back as `1.23`; `{"dailyCost": 0}` read back as `0.0`.

Send only `dailyCost` on update. Resending `campaignId`/`year`/`month` in a PATCH is untested, and those fields identify the record, so a rejected or re-keyed write is a real risk for no benefit. The full `{campaignId, year, month, dailyCost}` body belongs to **create** (`POST /costs`), where it is required.

### Campaigns and matching

`GET /marketing/v2/tenant/{tenant_id}/campaigns` returns `id`, `name`, `active`, `isDefaultCampaign`, a `category` object, plus `source`/`medium` fields that are frequently `null`.

Spreadsheets will carry campaign **names**, and the API needs campaign **IDs**, so name resolution is a core problem, not an afterthought. Names are not guaranteed unique or stable, and many carry category strings like `Search||Google||NoCost`. Plan for ambiguous and unmatched names as a normal case that the user resolves, not as a crash.

Scale, measured on `acme_east` 2026-08-13: **2,489 campaigns, 2,470 distinct names after normalization, so 19 names are shared by more than one campaign.** Ambiguity is a live condition in this tenant, not a hypothetical. The resolver's refusal to auto-resolve a name matching two campaigns is load-bearing.

### Tenants

Four tenants are configured in the `servicetitan-local` MCP server: `acme_east`, `acme_west`, `northwind`, `globex`. Treat multi-tenant as a requirement rather than a later addition.

## Working with the ServiceTitan API

- The `servicetitan-local` and `servicetitan-multi` MCP servers are available for **read-only exploration** of live data. Use them to check real shapes instead of guessing.
- These MCP servers point at **production tenants with real business data**. Never issue a POST, PATCH, or PUT through them, including via the `servicetitan_api_call` escape hatch, without explicit per-instance approval from Justin. Reads are fine.
- Main API allows roughly 60 req/sec (the MCP client self-limits near 30). Reporting endpoints are capped near 5 req/min, so avoid them.
- Trust the `hasMore=` footer over the returned item count when paging.

## Conventions

- Never commit credentials. ServiceTitan auth uses a client ID, client secret, app key, and tenant ID; all of them belong in environment variables or an ignored local file.
- Record anything verified against the live API in this file with the date, so later sessions do not re-derive it.

## Handoff

`next_steps.md` is the working handoff. Read it after syncing with GitHub and update it before ending a session with material work outstanding.
