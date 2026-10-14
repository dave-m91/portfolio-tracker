"""Tests for the monthly DCA purchase-plan engine."""

from decimal import Decimal

from portfolio.models import (
    DEFAULT_TARGET_ALLOCATIONS,
    AllocationCategory,
    AssetType,
    TargetAllocation,
    normalize_target_weights,
)
from portfolio.portfolio import PositionSummary
from portfolio.rebalancer import calculate_monthly_plan


def _allocations() -> list[TargetAllocation]:
    return [TargetAllocation(**entry) for entry in DEFAULT_TARGET_ALLOCATIONS]


def _position(
    ticker: str,
    name: str,
    currency: str,
    current_price: Decimal,
    value_pln: Decimal,
) -> PositionSummary:
    return PositionSummary(
        ticker=ticker,
        name=name,
        asset_type=AssetType.ETF if currency == "EUR" else AssetType.AKCJA,
        currency=currency,
        quantity=Decimal("0"),
        avg_buy_price=Decimal("0"),
        current_price=current_price,
        value_local=value_pln,
        value_pln=value_pln,
        pnl_local=Decimal("0"),
        pnl_pln=Decimal("0"),
        pnl_pct=Decimal("0"),
    )


def _run_plan(positions: list[PositionSummary]) -> dict:
    return calculate_monthly_plan(
        budget=Decimal("1000.0"),
        eur_pln_rate=Decimal("4.5"),
        positions=positions,
        allocations=_allocations(),
    )


# Portfolio after the previous tranche: ETFs + NWG + BFT were just bought.
_AFTER_FIRST_TRANCHE = [
    _position("QDVE.DE", "S&P 500 IT Sector", "EUR", Decimal("48.31"), Decimal("350.00")),
    _position("QDVF.DE", "S&P 500 Energy Sector", "EUR", Decimal("12.564"), Decimal("100.00")),
    _position("NWG.WA", "Newag", "PLN", Decimal("103.60"), Decimal("275.00")),
    _position("BFT.WA", "Benefit Systems", "PLN", Decimal("5100.00"), Decimal("275.00")),
]


def test_plan_rotates_away_from_recently_bought() -> None:
    plan = _run_plan(_AFTER_FIRST_TRANCHE)

    buys = plan["buys"]
    assert len(buys) == 4

    stock_tickers = [b.ticker for b in buys if b.ticker.endswith(".WA")]
    # NWG and BFT were just bought -> now overweight, so two *other* stocks win.
    assert stock_tickers == ["CDR.WA", "DNP.WA"]
    assert "NWG.WA" not in stock_tickers
    assert "BFT.WA" not in stock_tickers

    # Energy slot (canonical IUES.DE) resolves to the actual XTB ticker QDVF.DE.
    etf_tickers = {b.ticker for b in buys if b.ticker.endswith(".DE")}
    assert etf_tickers == {"QDVE.DE", "QDVF.DE"}

    # Units: held ETFs get a price-based estimate; unheld stocks have none.
    by_ticker = {b.ticker: b for b in buys}
    assert by_ticker["QDVE.DE"].estimated_units > 0
    assert by_ticker["QDVF.DE"].estimated_units > 0
    assert by_ticker["CDR.WA"].estimated_units == Decimal("0")


def test_plan_total_equals_budget() -> None:
    plan = _run_plan(_AFTER_FIRST_TRANCHE)

    assert plan["total"] == Decimal("1000.00")
    assert sum(b.amount_pln for b in plan["buys"]) == Decimal("1000.00")

    by_ticker = {b.ticker: b.amount_pln for b in plan["buys"]}
    assert by_ticker["QDVE.DE"] == Decimal("350.00")  # S&P 500 IT
    assert by_ticker["QDVF.DE"] == Decimal("100.00")  # S&P 500 Energy
    stock_amounts = [b.amount_pln for b in plan["buys"] if b.ticker.endswith(".WA")]
    assert stock_amounts == [Decimal("275.00"), Decimal("275.00")]


def test_default_allocations_sum_to_100() -> None:
    normalized = normalize_target_weights(DEFAULT_TARGET_ALLOCATIONS)
    assert sum(e["target_weight"] for e in normalized) == Decimal("100")


def test_normalize_scales_non_100_weights() -> None:
    entries = [
        {
            "ticker": "A.WA",
            "name": "A",
            "category": AllocationCategory.AKCJA,
            "target_weight": Decimal("50"),
            "is_fixed": False,
        },
        {
            "ticker": "B.WA",
            "name": "B",
            "category": AllocationCategory.AKCJA,
            "target_weight": Decimal("150"),
            "is_fixed": False,
        },
    ]
    normalized = normalize_target_weights(entries)
    assert [e["target_weight"] for e in normalized] == [
        Decimal("25"),
        Decimal("75"),
    ]
