# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

### Writes are an upsert, not a create

ServiceTitan does **not** pre-create cost rows for every campaign. This is confirmed by counterexample within one tenant:

- Campaign `1000001` has records going back to 2022-01, all at `dailyCost: 0.0`.
- Campaign `1000002` ("Google") has **zero** cost records.

So for every `(campaign, year, month)` the tool must look up an existing record and update it, or create one when absent. Blind creates risk duplicate or rejected rows.

Both operations exist in the Marketing v2 API (`create` and `update` on costs). The create body is `{campaignId, year, month, dailyCost}`. Confirm the exact update verb and path against the ServiceTitan developer portal before writing that code; the portal requires an authenticated session, so it could not be read directly here.

### Campaigns and matching

`GET /marketing/v2/tenant/{tenant_id}/campaigns` returns `id`, `name`, `active`, `isDefaultCampaign`, a `category` object, plus `source`/`medium` fields that are frequently `null`.

Spreadsheets will carry campaign **names**, and the API needs campaign **IDs**, so name resolution is a core problem, not an afterthought. Names are not guaranteed unique or stable, and many carry category strings like `Search||Google||NoCost`. Plan for ambiguous and unmatched names as a normal case that the user resolves, not as a crash.

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
