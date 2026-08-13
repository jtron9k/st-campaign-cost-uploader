"""Resolve spreadsheet campaign names to ServiceTitan campaign IDs.

Three tiers, in strict order:
  1. exact match on the normalized name
  2. the persisted alias map
  3. ranked fuzzy candidates, which are offered but never auto-applied

An alias outranks any fuzzy score because the alias records a human decision.
An ambiguous exact match (two campaigns sharing a name) is deliberately not
resolved; the operator picks.
"""

from __future__ import annotations

from collections import defaultdict

from rapidfuzz import fuzz

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.models import Campaign, Candidate, MatchKind, Resolution, SheetRow
from st_cost_uploader.normalize import normalize_name

FUZZY_FLOOR = 60
MAX_CANDIDATES = 5


def resolve(
    rows: list[SheetRow],
    campaigns: list[Campaign],
    alias_store: AliasStore,
) -> list[Resolution]:
    by_name: dict[str, list[Campaign]] = defaultdict(list)
    for campaign in campaigns:
        by_name[normalize_name(campaign.name)].append(campaign)
    by_id = {c.id: c for c in campaigns}

    seen: dict[str, Resolution] = {}
    results: list[Resolution] = []

    for row in rows:
        key = normalize_name(row.campaign_name)
        cached = seen.get(key)
        if cached is not None:
            results.append(
                Resolution(
                    row=row,
                    kind=cached.kind,
                    campaign_id=cached.campaign_id,
                    campaign_name=cached.campaign_name,
                    candidates=cached.candidates,
                )
            )
            continue

        resolution = _resolve_one(row, key, by_name, by_id, alias_store, campaigns)
        seen[key] = resolution
        results.append(resolution)

    return results


def _resolve_one(
    row: SheetRow,
    key: str,
    by_name: dict[str, list[Campaign]],
    by_id: dict[int, Campaign],
    alias_store: AliasStore,
    campaigns: list[Campaign],
) -> Resolution:
    exact = by_name.get(key, [])
    if len(exact) == 1:
        return Resolution(row, MatchKind.EXACT, exact[0].id, exact[0].name)

    alias_id = alias_store.get(row.campaign_name)
    if alias_id is not None and alias_id in by_id:
        target = by_id[alias_id]
        return Resolution(row, MatchKind.ALIAS, target.id, target.name)

    candidates = _rank(row.campaign_name, campaigns)
    if candidates:
        return Resolution(row, MatchKind.FUZZY, None, None, candidates)

    return Resolution(row, MatchKind.NONE, None, None, ())


def _rank(sheet_name: str, campaigns: list[Campaign]) -> tuple[Candidate, ...]:
    normalized = normalize_name(sheet_name)
    scored: list[Candidate] = []
    for campaign in campaigns:
        score = round(fuzz.token_set_ratio(normalized, normalize_name(campaign.name)))
        if score >= FUZZY_FLOOR:
            scored.append(Candidate(campaign.id, campaign.name, score))

    scored.sort(key=lambda c: (-c.score, c.campaign_name))
    return tuple(scored[:MAX_CANDIDATES])
