"""Credential loading. Mirrors the variable naming used by the
servicetitan-local MCP server so one .env can serve both."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuration is missing or incomplete."""


@dataclass(frozen=True)
class TenantConfig:
    name: str
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    app_key: str = field(repr=False)


def _require(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"{key} is missing or blank. See .env.example.")
    return value


def load_tenants(env: Mapping[str, str] | None = None) -> dict[str, TenantConfig]:
    """Build one TenantConfig per slug listed in ST_TENANTS."""
    if env is None:
        load_dotenv()
        env = os.environ

    raw = (env.get("ST_TENANTS") or "").strip()
    if not raw:
        raise ConfigError("ST_TENANTS is missing or blank. See .env.example.")

    tenants: dict[str, TenantConfig] = {}
    for slug in (s.strip() for s in raw.split(",")):
        if not slug:
            continue
        prefix = f"ST_TENANT_{slug.upper()}"
        tenants[slug] = TenantConfig(
            name=slug,
            tenant_id=_require(env, f"{prefix}_ID"),
            client_id=_require(env, f"{prefix}_CLIENT_ID"),
            client_secret=_require(env, f"{prefix}_CLIENT_SECRET"),
            app_key=_require(env, f"{prefix}_APP_KEY"),
        )

    if not tenants:
        raise ConfigError("ST_TENANTS listed no usable tenant names.")
    return tenants
