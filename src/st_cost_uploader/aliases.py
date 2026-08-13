"""Per-tenant map from spreadsheet campaign name to ServiceTitan campaign ID.

Stored as JSON under aliases/<tenant>.json and tracked in git. It holds no
secrets, and sharing it means a name confirmed once is resolved for everyone.
"""

from __future__ import annotations

import json
from pathlib import Path

from st_cost_uploader.normalize import normalize_name

DEFAULT_DIR = Path("aliases")


class AliasStore:
    def __init__(self, path: Path, mapping: dict[str, int]) -> None:
        self._path = path
        self._mapping = mapping

    @classmethod
    def load(cls, tenant: str, base_dir: Path | str = DEFAULT_DIR) -> AliasStore:
        path = Path(base_dir) / f"{tenant}.json"
        if not path.exists():
            return cls(path, {})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return cls(path, {str(k): int(v) for k, v in payload.items()})

    def get(self, sheet_name: str) -> int | None:
        return self._mapping.get(normalize_name(sheet_name))

    def set(self, sheet_name: str, campaign_id: int) -> None:
        self._mapping[normalize_name(sheet_name)] = int(campaign_id)

    def as_dict(self) -> dict[str, int]:
        return dict(self._mapping)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(dict(sorted(self._mapping.items())), indent=2) + "\n"
        self._path.write_text(payload, encoding="utf-8")
