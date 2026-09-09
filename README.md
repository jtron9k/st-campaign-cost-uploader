# ST Campaign Cost Uploader

Bulk-upload marketing campaign costs into ServiceTitan from a spreadsheet, or from rows pasted straight out of one.

## Why

ServiceTitan's built-in campaign cost interface takes one value per campaign, per month, one at a time. Worse, the field it stores is a *daily* cost even though the record covers a month, so anyone working from a monthly ad-spend number has to divide by the length of that month before typing it in. At any real campaign count this is slow and easy to get wrong.

This tool takes a spreadsheet of monthly campaign spend and writes it to ServiceTitan through the Marketing v2 API. It handles the monthly-to-daily conversion, matches your spreadsheet's campaign names to ServiceTitan campaign IDs, and decides per row whether to create a cost record or update the one that exists.

## What you need

- A ServiceTitan API application with access to the Marketing v2 endpoints. You will need the tenant ID, client ID, client secret, and app key for each tenant you manage.
- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

## Running it

```bash
cp .env.example .env    # then fill in your ServiceTitan credentials
uv sync --extra dev
uv run uvicorn st_cost_uploader.web.app:app --port 8000
```

Open http://127.0.0.1:8000, pick a tenant, then upload a `.xlsx` or `.csv`, or paste rows copied from Excel or Google Sheets. Sample files in both supported layouts are linked from the upload screen.

The flow is four screens:

1. **Upload.** Read the file, detect the layout, show how columns were mapped.
2. **Resolve.** Match spreadsheet campaign names to ServiceTitan campaigns. Exact matches and previously confirmed aliases resolve on their own; fuzzy suggestions and unmatched names wait for you.
3. **Preview.** Every planned write with its daily cost, the prior value it replaces, and the rounding residual. Nothing has been sent yet.
4. **Results.** What was written, what failed, and why.

Every write is recorded in `logs/writes.jsonl`. Confirmed name matches are saved per tenant under `aliases/` and reused on the next upload.

## The daily-cost trap

ServiceTitan stores `dailyCost` to the cent. Dividing a monthly total by the days in that month and rounding means the number ServiceTitan holds will reconstruct to slightly less or more than what you entered, by at most about fifteen cents per campaign-month. The preview shows this residual per row so it is never a surprise. See [`AGENTS.md`](AGENTS.md) for everything verified about the API's behavior.

## Tests

```bash
uv run pytest
```

No test touches the network. The ServiceTitan client is exercised against `httpx.MockTransport` and the web layer against a stub client.

## Safety

This tool writes to production ServiceTitan tenants. Nothing reaches ServiceTitan until you approve the preview, and cost records cannot be deleted through the API once created, so read the preview before you click.

The tenant names, campaign IDs, and record IDs that appear in tests and docs are placeholders.

## License

MIT. See [`LICENSE`](LICENSE).
