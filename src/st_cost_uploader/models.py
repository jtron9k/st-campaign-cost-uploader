"""Domain types. Frozen, no behavior beyond derived properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

PeriodKey = tuple[int, int, int]


@dataclass(frozen=True)
class SheetRow:
    """One canonical row, whatever layout it came from."""

    campaign_name: str
    year: int
    month: int
    monthly_total: Decimal
    source_row: int


@dataclass(frozen=True)
class ParseError:
    source_row: int
    message: str


@dataclass(frozen=True)
class ParseResult:
    layout: str
    rows: tuple[SheetRow, ...]
    errors: tuple[ParseError, ...]
    column_mapping: dict[str, str]


@dataclass(frozen=True)
class Campaign:
    id: int
    name: str
    active: bool


@dataclass(frozen=True)
class CostRecord:
    id: int
    campaign_id: int
    year: int
    month: int
    daily_cost: Decimal

    @property
    def period_key(self) -> PeriodKey:
        return (self.campaign_id, self.year, self.month)


class MatchKind(str, Enum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass(frozen=True)
class Candidate:
    campaign_id: int
    campaign_name: str
    score: int


@dataclass(frozen=True)
class Resolution:
    row: SheetRow
    kind: MatchKind
    campaign_id: int | None
    campaign_name: str | None
    candidates: tuple[Candidate, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.campaign_id is not None


@dataclass(frozen=True)
class CostConversion:
    monthly_total: Decimal
    days_in_month: int
    daily_cost: Decimal
    reconstructed_total: Decimal
    residual: Decimal


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    NO_CHANGE = "no_change"


@dataclass(frozen=True)
class PlannedWrite:
    campaign_id: int
    campaign_name: str
    sheet_name: str
    year: int
    month: int
    action: Action
    conversion: CostConversion
    cost_id: int | None
    prior_daily_cost: Decimal | None

    @property
    def period_key(self) -> PeriodKey:
        return (self.campaign_id, self.year, self.month)

    @property
    def overwrites_nonzero(self) -> bool:
        return (
            self.action is Action.UPDATE
            and self.prior_daily_cost is not None
            and self.prior_daily_cost != Decimal("0")
        )


@dataclass(frozen=True)
class WriteOutcome:
    plan: PlannedWrite
    ok: bool
    error: str | None = None
