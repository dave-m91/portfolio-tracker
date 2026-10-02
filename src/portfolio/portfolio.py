"""Portfolio operations: recording trades and computing positions."""

from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlmodel import select

from .db import get_session
from .fetcher import fetch_eur_pln_rate
from .models import Asset, PriceHistory, Transaction, TransactionType

_CENT = Decimal("0.01")


def _quantize(value: Decimal) -> Decimal:
    """Round a monetary/value to two decimal places (cents)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PositionSummary:
    """Computed state of a single asset position."""

    ticker: str
    name: str
    currency: str
    quantity: Decimal
    avg_buy_price: Decimal  # break-even per unit incl. fees (asset currency)
    current_price: Decimal  # latest close (asset currency)
    value_local: Decimal
    value_pln: Decimal
    pnl_local: Decimal
    pnl_pln: Decimal
    pnl_pct: Decimal


def record_transaction(
    asset_ticker: str,
    date: date_type,
    type: TransactionType,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal = Decimal("0"),
) -> None:
    """Insert a buy/sell transaction for the asset with ``asset_ticker``.

    Raises ``ValueError`` when the ticker is unknown.
    """
    session = get_session()
    try:
        asset = session.exec(
            select(Asset).where(Asset.ticker == asset_ticker)
        ).first()
        if asset is None:
            raise ValueError(f"Nieznany ticker: {asset_ticker}")
        session.add(
            Transaction(
                asset_id=asset.id,
                date=date,
                type=type,
                quantity=quantity,
                price=price,
                fee=fee,
            )
        )
        session.commit()
    finally:
        session.close()


def get_portfolio_summary(
    eur_pln_rate: Optional[Decimal] = None,
) -> list[PositionSummary]:
    """Compute per-asset positions and unrealized P&L.

    ``eur_pln_rate`` may be passed explicitly (e.g. in tests). When omitted and
    at least one EUR-denominated asset exists, the rate is fetched from NBP.
    """
    session = get_session()
    try:
        assets = session.exec(select(Asset).order_by(Asset.id)).all()

        raw = []
        for asset in assets:
            transactions = session.exec(
                select(Transaction).where(Transaction.asset_id == asset.id)
            ).all()
            buy_qty = sum(
                (t.quantity for t in transactions if t.type == TransactionType.KUPNO),
                Decimal("0"),
            )
            buy_cost = sum(
                (
                    t.quantity * t.price + t.fee
                    for t in transactions
                    if t.type == TransactionType.KUPNO
                ),
                Decimal("0"),
            )
            sell_qty = sum(
                (
                    t.quantity
                    for t in transactions
                    if t.type == TransactionType.SPRZEDAZ
                ),
                Decimal("0"),
            )
            net_qty = buy_qty - sell_qty
            avg_buy = buy_cost / buy_qty if buy_qty else Decimal("0")

            latest = session.exec(
                select(PriceHistory)
                .where(PriceHistory.asset_id == asset.id)
                .order_by(PriceHistory.date.desc(), PriceHistory.id.desc())
                .limit(1)
            ).first()
            current = latest.close_price if latest else Decimal("0")
            raw.append((asset, net_qty, avg_buy, current))
    finally:
        session.close()

    needs_eur = any(asset.currency == "EUR" for asset, *_ in raw)
    if eur_pln_rate is None:
        if needs_eur:
            eur_pln_rate = fetch_eur_pln_rate()
            if eur_pln_rate is None:
                raise RuntimeError("Nie udało się pobrać kursu EUR/PLN z NBP")
        else:
            eur_pln_rate = Decimal("1")

    summaries = []
    for asset, net_qty, avg_buy, current in raw:
        rate = eur_pln_rate if asset.currency == "EUR" else Decimal("1")
        value_local = net_qty * current
        cost_local = net_qty * avg_buy
        pnl_local = value_local - cost_local
        pnl_pct = (pnl_local / cost_local * 100) if cost_local else Decimal("0")
        summaries.append(
            PositionSummary(
                ticker=asset.ticker,
                name=asset.name,
                currency=asset.currency,
                quantity=net_qty,
                avg_buy_price=_quantize(avg_buy),
                current_price=_quantize(current),
                value_local=_quantize(value_local),
                value_pln=_quantize(value_local * rate),
                pnl_local=_quantize(pnl_local),
                pnl_pln=_quantize(pnl_local * rate),
                pnl_pct=_quantize(pnl_pct),
            )
        )
    return summaries