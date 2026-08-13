from decimal import Decimal

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.models import Campaign, MatchKind, SheetRow
from st_cost_uploader.resolver import FUZZY_FLOOR, MAX_CANDIDATES, resolve

CAMPAIGNS = [
    Campaign(id=1, name="Search||Google||Brand", active=True),
    Campaign(id=2, name="Facebook Retargeting", active=True),
    Campaign(id=3, name="Yelp", active=True),
    Campaign(id=4, name="Local Services Ads", active=True),
    Campaign(id=5, name="Angi Leads", active=False),
]


def _row(name: str) -> SheetRow:
    return SheetRow(name, 2026, 2, Decimal("100"), 2)


def _store(tmp_path, pairs=()):
    store = AliasStore.load("t", tmp_path)
    for name, cid in pairs:
        store.set(name, cid)
    return store


def test_exact_match(tmp_path):
    [res] = resolve([_row("Yelp")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.EXACT
    assert res.campaign_id == 3


def test_exact_match_ignores_case_and_spacing(tmp_path):
    [res] = resolve([_row("  facebook   retargeting ")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.EXACT
    assert res.campaign_id == 2


def test_alias_resolves_when_name_differs(tmp_path):
    store = _store(tmp_path, [("Yelp Ads", 3)])
    [res] = resolve([_row("Yelp Ads")], CAMPAIGNS, store)
    assert res.kind is MatchKind.ALIAS
    assert res.campaign_id == 3
    assert res.campaign_name == "Yelp"


def test_alias_beats_a_higher_scoring_fuzzy_candidate(tmp_path):
    # "Angi" is not any campaign's exact name, and fuzzy-matches "Angi Leads"
    # (id 5) strongly. The alias records a human decision pointing at 3, and
    # a recorded decision outranks any score.
    store = _store(tmp_path, [("Angi", 3)])
    [res] = resolve([_row("Angi")], CAMPAIGNS, store)
    assert res.kind is MatchKind.ALIAS
    assert res.campaign_id == 3


def test_without_an_alias_the_same_name_matches_fuzzily(tmp_path):
    [res] = resolve([_row("Angi")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.FUZZY
    assert res.candidates[0].campaign_id == 5


def test_alias_pointing_at_a_missing_campaign_falls_through(tmp_path):
    store = _store(tmp_path, [("Ghost", 9999)])
    [res] = resolve([_row("Ghost")], CAMPAIGNS, store)
    assert res.kind is not MatchKind.ALIAS
    assert res.campaign_id is None


def test_fuzzy_offers_candidates_without_resolving(tmp_path):
    [res] = resolve([_row("Facebook Retarget")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.FUZZY
    assert res.campaign_id is None
    assert res.is_resolved is False
    assert res.candidates[0].campaign_id == 2
    assert res.candidates[0].score >= FUZZY_FLOOR


def test_candidates_are_sorted_by_score_descending(tmp_path):
    # CAMPAIGNS alone doesn't clear the fuzzy floor with more than one
    # candidate for any single sheet name, which would make the sort
    # assertion below vacuously true ([] == []). Use a fixture with several
    # genuinely similar names so more than one candidate is actually scored.
    similar = [
        Campaign(id=1, name="Google Search Brand", active=True),
        Campaign(id=2, name="Google Search Generic", active=True),
        Campaign(id=3, name="Google Display", active=True),
    ]
    [res] = resolve([_row("Google Search")], similar, _store(tmp_path))
    scores = [c.score for c in res.candidates]
    assert len(scores) > 1
    assert scores == sorted(scores, reverse=True)


def test_candidates_are_capped(tmp_path):
    many = [Campaign(id=i, name=f"Google Campaign {i}", active=True) for i in range(20)]
    [res] = resolve([_row("Google Campaign")], many, _store(tmp_path))
    assert len(res.candidates) <= MAX_CANDIDATES


def test_no_match_below_floor(tmp_path):
    [res] = resolve([_row("zzzzzzzzzz qqqqqqqq")], CAMPAIGNS, _store(tmp_path))
    assert res.kind is MatchKind.NONE
    assert res.candidates == ()


def test_inactive_campaigns_are_still_matchable(tmp_path):
    # Historical months legitimately target campaigns that are now off.
    [res] = resolve([_row("Angi Leads")], CAMPAIGNS, _store(tmp_path))
    assert res.campaign_id == 5


def test_duplicate_names_do_not_exact_match(tmp_path):
    dupes = [
        Campaign(id=10, name="Google", active=True),
        Campaign(id=11, name="Google", active=True),
    ]
    [res] = resolve([_row("Google")], dupes, _store(tmp_path))
    assert res.kind is MatchKind.FUZZY
    assert res.campaign_id is None
    assert {c.campaign_id for c in res.candidates} == {10, 11}


def test_every_row_gets_a_resolution(tmp_path):
    rows = [_row("Yelp"), _row("Unknown Thing"), _row("Facebook Retargeting")]
    assert len(resolve(rows, CAMPAIGNS, _store(tmp_path))) == 3
