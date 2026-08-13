"""Read-only live probe against a ServiceTitan tenant.

This exists so live data can be inspected from any environment that has
credentials, without depending on the servicetitan-local MCP server. It is
the tool used to verify a write landed, per next_steps.md's "Still owed".

Read-only by construction: it calls only the client's GET methods, and
never imports create_cost or update_cost. Keep it that way. Writes belong
in the app, behind the preview gate, where they are audited.

    uv run python scripts/st_probe.py tenants
    uv run python scripts/st_probe.py campaigns acme_east --match google
    uv run python scripts/st_probe.py costs acme_east --campaign 1000001
    uv run python scripts/st_probe.py cost acme_east --id 500000001
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from st_cost_uploader.client import ServiceTitanClient, ServiceTitanError
from st_cost_uploader.config import ConfigError, load_tenants


def _tenant(slug: str):
    tenants = load_tenants()
    if slug not in tenants:
        raise ConfigError(
            f"Tenant {slug!r} is not configured. ST_TENANTS lists: "
            f"{', '.join(sorted(tenants)) or '(none)'}"
        )
    return tenants[slug]


async def _campaigns(args: argparse.Namespace) -> int:
    async with ServiceTitanClient(_tenant(args.tenant)) as client:
        campaigns = await client.list_campaigns()

    needle = (args.match or "").casefold()
    shown = [c for c in campaigns if needle in c.name.casefold()]
    for c in sorted(shown, key=lambda c: c.name.casefold()):
        print(f"{c.id:>12}  {'active' if c.active else '  --  '}  {c.name}")
    print(f"\n{len(shown)} of {len(campaigns)} campaigns", file=sys.stderr)
    return 0


async def _costs(args: argparse.Namespace) -> int:
    async with ServiceTitanClient(_tenant(args.tenant)) as client:
        found = await client.list_costs_for_campaigns([args.campaign])

    records = sorted(found.values(), key=lambda r: (r.year, r.month))
    if args.year is not None:
        records = [r for r in records if r.year == args.year]

    print(f"{'id':>12}  {'period':>7}  {'dailyCost':>12}")
    for r in records:
        print(f"{r.id:>12}  {r.year}-{r.month:02d}  {r.daily_cost:>12}")
    print(f"\n{len(records)} cost records", file=sys.stderr)
    return 0


async def _cost(args: argparse.Namespace) -> int:
    async with ServiceTitanClient(_tenant(args.tenant)) as client:
        r = await client.get_cost(args.id)

    print(
        f"id={r.id} campaignId={r.campaign_id} "
        f"year={r.year} month={r.month} dailyCost={r.daily_cost}"
    )
    return 0


async def _tenants(_: argparse.Namespace) -> int:
    # Names only. Never print tenant_id, client_id, secret, or app key --
    # this output goes into terminals and transcripts.
    for name in sorted(load_tenants()):
        print(name)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tenants", help="list configured tenant slugs").set_defaults(fn=_tenants)

    p = sub.add_parser("campaigns", help="list campaigns")
    p.add_argument("tenant")
    p.add_argument("--match", help="case-insensitive substring filter on name")
    p.set_defaults(fn=_campaigns)

    p = sub.add_parser("costs", help="list cost records for one campaign")
    p.add_argument("tenant")
    p.add_argument("--campaign", type=int, required=True)
    p.add_argument("--year", type=int)
    p.set_defaults(fn=_costs)

    p = sub.add_parser("cost", help="fetch one cost record by id")
    p.add_argument("tenant")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(fn=_cost)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(args.fn(args))
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    except ServiceTitanError as exc:
        print(f"servicetitan: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
