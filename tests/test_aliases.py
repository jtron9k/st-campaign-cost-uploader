import json

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.normalize import normalize_name


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_name("  Google   Search  ") == "google search"
    assert normalize_name("YELP") == "yelp"
    assert normalize_name("Search||Google||Brand") == "search||google||brand"


def test_normalize_handles_non_breaking_space():
    assert normalize_name("Google Ads") == "google ads"


def test_load_creates_empty_store_when_file_absent(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    assert store.get("anything") is None
    assert store.as_dict() == {}


def test_set_then_get_roundtrips_through_disk(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    store.set("Yelp Ads", 1000001)
    store.save()

    reloaded = AliasStore.load("acme_east", tmp_path)
    assert reloaded.get("Yelp Ads") == 1000001


def test_lookup_is_normalized(tmp_path):
    store = AliasStore.load("acme_east", tmp_path)
    store.set("Yelp   Ads", 99)
    assert store.get("yelp ads") == 99
    assert store.get("  YELP ADS ") == 99


def test_file_lands_at_expected_path_and_is_readable_json(tmp_path):
    store = AliasStore.load("northwind", tmp_path)
    store.set("Nextdoor - Local", 123)
    store.save()

    path = tmp_path / "northwind.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload == {"nextdoor - local": 123}


def test_tenants_do_not_share_aliases(tmp_path):
    a = AliasStore.load("acme_east", tmp_path)
    a.set("Yelp", 1)
    a.save()

    b = AliasStore.load("globex", tmp_path)
    assert b.get("Yelp") is None


def test_corrupt_file_raises_rather_than_silently_emptying(tmp_path):
    (tmp_path / "acme_east.json").write_text("{not json")
    try:
        AliasStore.load("acme_east", tmp_path)
    except ValueError as exc:
        assert "acme_east.json" in str(exc)
    else:
        raise AssertionError("expected ValueError")
