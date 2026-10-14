"""Monthly DCA purchase-plan engine (target-weight rebalancing)."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlmodel import select

from .db import get_session
from .fetcher import fetch_eur_pln_rate
from .models import (
    DEFAULT_TARGET_ALLOCATIONS,
    TARGET_TICKER_ALIASES,
    TargetAllocation,
    Transaction,
    normalize_target_weights,
)
from .portfolio import PositionSummary, get_portfolio_summary

_CENT = Decimal("0.01")
_UNIT = Decimal("0.0001")
_STOCK_PICKS = 2


@dataclass(frozen=True)
class BuyRecommendation:
    """A single recommended purchase for the next tranche."""

    ticker: str
    name: str
    amount_pln: Decimal
    estimated_units: Decimal
    target_weight: Decimal
    current_weight: Decimal


@dataclass(frozen=True)
class ReserveItem:
    """A stock waiting for rotation."""

    ticker: str
    name: str
    target_weight: Decimal
    current_weight: Decimal
    gap: Decimal


@dataclass(frozen=True)
class AllocationComparison:
    """Current vs target weight for one allocation slot."""

    ticker: str
    name: str
    category: str
    target_weight: Decimal
    current_weight: Decimal


def load_target_allocations() -> list[TargetAllocation]:
    """Read target allocations from the DB, falling back to the defaults."""
    session = get_session()
    try:
        rows = session.exec(
            select(TargetAllocation).order_by(TargetAllocation.id)
        ).all()
        if rows:
            return list(rows)
    finally:
        session.close()
    return [
        TargetAllocation(**entry)
        for entry in normalize_target_weights(DEFAULT_TARGET_ALLOCATIONS)
    ]


def _resolve_slot(
    alloc: TargetAllocation, pos_by_ticker: dict
) -> tuple[str, Optional[PositionSummary]]:
    """Resolve a target allocation to its actual ticker and current position."""
    if alloc.ticker in pos_by_ticker:
        return alloc.ticker, pos_by_ticker[alloc.ticker]
    for alias, target in TARGET_TICKER_ALIASES.items():
        if target == alloc.ticker and alias in pos_by_ticker:
            return alias, pos_by_ticker[alias]
    return alloc.ticker, None


def _current_weight(
    pos: Optional[PositionSummary], total_value: Decimal
) -> Decimal:
    if pos is None or total_value == 0:
        return Decimal("0")
    return pos.value_pln / total_value * 100


def _estimated_units(
    amount_pln: Decimal,
    pos: Optional[PositionSummary],
    eur_pln_rate: Optional[Decimal],
) -> Decimal:
    """Convert a PLN amount into an estimated unit count at the last close."""
    if pos is None:
        return Decimal("0")
    price_pln = pos.current_price
    if pos.currency == "EUR":
        price_pln = price_pln * (eur_pln_rate or Decimal("0"))
    if price_pln == 0:
        return Decimal("0")
    return (amount_pln / price_pln).quantize(_UNIT, rounding=ROUND_HALF_UP)


def _build_buy(
    alloc: TargetAllocation,
    actual_ticker: str,
    pos: Optional[PositionSummary],
    amount: Decimal,
    total_value: Decimal,
    eur_pln_rate: Optional[Decimal],
) -> BuyRecommendation:
    return BuyRecommendation(
        ticker=actual_ticker,
        name=pos.name if pos else alloc.name,
        amount_pln=amount,
        estimated_units=_estimated_units(amount, pos, eur_pln_rate),
        target_weight=alloc.target_weight,
        current_weight=_current_weight(pos, total_value).quantize(_CENT),
    )


def _build_comparison(
    alloc: TargetAllocation,
    pos_by_ticker: dict,
    total_value: Decimal,
) -> AllocationComparison:
    actual_ticker, pos = _resolve_slot(alloc, pos_by_ticker)
    return AllocationComparison(
        ticker=actual_ticker,
        name=pos.name if pos else alloc.name,
        category=alloc.category.value,
        target_weight=alloc.target_weight,
        current_weight=_current_weight(pos, total_value).quantize(_CENT),
    )


def calculate_monthly_plan(
    budget: Decimal = Decimal("1000.0"),
    eur_pln_rate: Optional[Decimal] = None,
    positions: Optional[list[PositionSummary]] = None,
    allocations: Optional[list[TargetAllocation]] = None,
) -> dict:
    """Build the next DCA tranche.

    Returns a dict with ``buys`` (4 recommendations), ``reserve`` (the other 5
    stocks) and ``comparison`` (current vs target for every slot).
    """
    if positions is None:
        positions = get_portfolio_summary()
    if allocations is None:
        allocations = load_target_allocations()

    pos_by_ticker = {p.ticker: p for p in positions}
    total_value = sum((p.value_pln for p in positions), Decimal("0"))

    if eur_pln_rate is None:
        eur_pln_rate = (
            fetch_eur_pln_rate() or Decimal("0")
            if any(p.currency == "EUR" for p in positions)
            else Decimal("1")
        )

    fixed_allocations = [a for a in allocations if a.is_fixed]
    stock_allocations = [a for a in allocations if not a.is_fixed]

    # Fixed ETF amounts, proportional to their target weight (450 PLN total).
    fixed_total = Decimal("0")
    fixed_plans: list[tuple[TargetAllocation, Decimal]] = []
    for alloc in fixed_allocations:
        amount = (budget * alloc.target_weight / Decimal("100")).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        fixed_plans.append((alloc, amount))
        fixed_total += amount

    stock_budget = budget - fixed_total
    per_stock = (stock_budget / Decimal(str(_STOCK_PICKS))).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )

    # Gap per stock; pick the two most underweight.
    stock_rows: list[tuple] = []
    for alloc in stock_allocations:
        actual_ticker, pos = _resolve_slot(alloc, pos_by_ticker)
        current_weight = _current_weight(pos, total_value)
        gap = alloc.target_weight - current_weight
        stock_rows.append((alloc, actual_ticker, pos, current_weight, gap))

    stock_rows.sort(key=lambda row: (-row[4], row[0].ticker))
    selected = stock_rows[:_STOCK_PICKS]
    reserve = stock_rows[_STOCK_PICKS:]

    buys: list[BuyRecommendation] = []
    for alloc, amount in fixed_plans:
        actual_ticker, pos = _resolve_slot(alloc, pos_by_ticker)
        buys.append(
            _build_buy(alloc, actual_ticker, pos, amount, total_value, eur_pln_rate)
        )

    stock_amounts = [per_stock, stock_budget - per_stock]
    for (alloc, actual_ticker, pos, _current, _gap), amount in zip(
        selected, stock_amounts
    ):
        buys.append(
            _build_buy(alloc, actual_ticker, pos, amount, total_value, eur_pln_rate)
        )

    reserve_items = [
        ReserveItem(
            ticker=actual_ticker,
            name=pos.name if pos else alloc.name,
            target_weight=alloc.target_weight,
            current_weight=current_weight.quantize(_CENT),
            gap=gap.quantize(_CENT),
        )
        for alloc, actual_ticker, pos, current_weight, gap in reserve
    ]

    comparison = [
        _build_comparison(alloc, pos_by_ticker, total_value)
        for alloc in allocations
    ]

    return {
        "buys": buys,
        "reserve": reserve_items,
        "comparison": comparison,
        "total": sum((b.amount_pln for b in buys), Decimal("0")),
    }


def current_month_has_transactions(today: Optional[date] = None) -> bool:
    """Return True when a transaction already exists in the current month."""
    today = today or date.today()
    month_start = today.replace(day=1)
    session = get_session()
    try:
        return (
            session.exec(
                select(Transaction).where(Transaction.date >= month_start)
            ).first()
            is not None
        )
    finally:
        session.close()
