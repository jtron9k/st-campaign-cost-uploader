import pytest

from st_cost_uploader.config import ConfigError, load_tenants

BASE = {
    "ST_TENANTS": "acme_east,northwind",
    "ST_TENANT_ACME_EAST_ID": "769643737",
    "ST_TENANT_ACME_EAST_CLIENT_ID": "cid.aaa",
    "ST_TENANT_ACME_EAST_CLIENT_SECRET": "cs2.bbb",
    "ST_TENANT_ACME_EAST_APP_KEY": "ak1.ccc",
    "ST_TENANT_NORTHWIND_ID": "111",
    "ST_TENANT_NORTHWIND_CLIENT_ID": "cid.ddd",
    "ST_TENANT_NORTHWIND_CLIENT_SECRET": "cs2.eee",
    "ST_TENANT_NORTHWIND_APP_KEY": "ak1.fff",
}


def test_loads_every_listed_tenant():
    tenants = load_tenants(BASE)
    assert set(tenants) == {"acme_east", "northwind"}
    assert tenants["acme_east"].tenant_id == "769643737"
    assert tenants["northwind"].app_key == "ak1.fff"


def test_whitespace_in_the_tenant_list_is_tolerated():
    env = dict(BASE, ST_TENANTS=" acme_east , northwind ")
    assert set(load_tenants(env)) == {"acme_east", "northwind"}


def test_missing_tenant_list_raises():
    with pytest.raises(ConfigError, match="ST_TENANTS"):
        load_tenants({})


def test_missing_credential_names_the_variable():
    env = dict(BASE)
    del env["ST_TENANT_NORTHWIND_CLIENT_SECRET"]
    with pytest.raises(ConfigError, match="ST_TENANT_NORTHWIND_CLIENT_SECRET"):
        load_tenants(env)


def test_blank_credential_is_treated_as_missing():
    env = dict(BASE, ST_TENANT_NORTHWIND_APP_KEY="   ")
    with pytest.raises(ConfigError, match="ST_TENANT_NORTHWIND_APP_KEY"):
        load_tenants(env)


def test_repr_does_not_leak_the_secret():
    tenants = load_tenants(BASE)
    assert "cs2.bbb" not in repr(tenants["acme_east"])
