# ST Campaign Cost Uploader

Bulk-upload marketing campaign costs into ServiceTitan from a spreadsheet.

## Why

ServiceTitan's built-in campaign cost interface requires entering a value per campaign, per month, one at a time. Worse, the field it stores is a *daily* cost even though the record covers a month, so anyone working from a monthly ad-spend number has to divide by the length of that month before typing it in. At any real campaign count this is slow and easy to get wrong.

This tool takes a spreadsheet of campaign spend and writes it to ServiceTitan through the Marketing v2 API, handling the monthly-to-daily conversion, campaign name resolution, and create-vs-update logic.

## Status

Early. Scaffolding only, no application code yet. The stack is undecided.

See [`next_steps.md`](next_steps.md) for the current handoff and the open spec questions, and [`CLAUDE.md`](CLAUDE.md) for verified API behavior.

## Safety note

This tool writes to production ServiceTitan tenants. Any design must include a dry-run preview before writes.
