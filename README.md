# ST Campaign Cost Uploader

Bulk-upload marketing campaign costs into ServiceTitan from a spreadsheet.

## Why

ServiceTitan's built-in campaign cost interface requires entering a value per campaign, per month, one at a time. Worse, the field it stores is a *daily* cost even though the record covers a month, so anyone working from a monthly ad-spend number has to divide by the length of that month before typing it in. At any real campaign count this is slow and easy to get wrong.

This tool takes a spreadsheet of campaign spend and writes it to ServiceTitan through the Marketing v2 API, handling the monthly-to-daily conversion, campaign name resolution, and create-vs-update logic.

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

See [`next_steps.md`](next_steps.md) for the current handoff, and [`CLAUDE.md`](CLAUDE.md) for verified API behavior.

## Safety note

This tool writes to production ServiceTitan tenants. Any design must include a dry-run preview before writes.
