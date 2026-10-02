"""CLI entry point for the portfolio tracker.

Run with: ``python -m portfolio.main``
"""

from datetime import date
from decimal import Decimal

from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from .db import engine, init_db
from .fetcher import fetch_yesterday_close
from .models import Asset, AssetType, PriceHistory, Transaction, TransactionType
from .portfolio import PositionSummary, get_portfolio_summary, record_transaction

console = Console()

# Seed instruments: ticker -> (name, asset_type, currency)
_SEED_ASSETS = [
    ("CDR.WA", "CD Projekt", AssetType.AKCJA, "PLN"),
    ("VWCE.DE", "Vanguard FTSE All-World UCITS ETF", AssetType.ETF, "EUR"),
]

# Seed trades: (ticker, type, quantity, price, fee, date)
_SEED_TRANSACTIONS = [
    ("CDR.WA", TransactionType.KUPNO, Decimal("50"), Decimal("180.00"), Decimal("5"), date(2026, 9, 15)),
    ("VWCE.DE", TransactionType.KUPNO, Decimal("10"), Decimal("130.00"), Decimal("2"), date(2026, 9, 15)),
]


def seed_assets(session: Session) -> None:
    """Insert the sample assets when they are missing."""
    existing = {asset.ticker for asset in session.exec(select(Asset)).all()}
    for ticker, name, asset_type, currency in _SEED_ASSETS:
        if ticker in existing:
            continue
        session.add(
            Asset(
                ticker=ticker,
                name=name,
                asset_type=asset_type,
                currency=currency,
            )
        )
        existing.add(ticker)
    session.commit()


def seed_transactions() -> None:
    """Insert the sample trades through the public portfolio API (idempotent)."""
    with Session(engine) as session:
        asset_ids_with_tx = {
            transaction.asset_id
            for transaction in session.exec(select(Transaction)).all()
        }
        ticker_ids = {
            asset.ticker: asset.id for asset in session.exec(select(Asset)).all()
        }
    for ticker, tx_type, quantity, price, fee, tx_date in _SEED_TRANSACTIONS:
        if ticker_ids.get(ticker) not in asset_ids_with_tx:
            record_transaction(ticker, tx_date, tx_type, quantity, price, fee)


def fetch_and_store(session: Session) -> None:
    """Fetch latest closes for every asset and persist them."""
    for asset in session.exec(select(Asset)).all():
        result = fetch_yesterday_close(asset.ticker)
        if result is None:
            console.print(
                f"[yellow]Brak danych dla {asset.ticker}[/yellow]",
                highlight=False,
            )
            continue
        price_date, close = result
        session.add(
            PriceHistory(asset_id=asset.id, date=price_date, close_price=close)
        )
    session.commit()


def _fmt_money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _fmt_quantity(value: Decimal) -> str:
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_signed(value: Decimal, suffix: str = "") -> str:
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    color = "green" if value > 0 else ("red" if value < 0 else "dim")
    return f"[{color}]{sign}{abs(value):,.2f}{suffix}[/{color}]"


def render_portfolio(summary: list[PositionSummary]) -> None:
    """Render per-position and portfolio-total rows as a Rich table."""
    table = Table(title="Podsumowanie portfela")
    table.add_column("Ticker", style="cyan", no_wrap=True)
    table.add_column("Ilość", justify="right")
    table.add_column("Średnia cena zakupu", justify="right")
    table.add_column("Aktualny kurs", justify="right")
    table.add_column("Wartość (PLN)", justify="right")
    table.add_column("P&L (PLN)", justify="right")
    table.add_column("P&L (%)", justify="right")

    total_value = Decimal("0")
    total_pnl = Decimal("0")
    for position in summary:
        total_value += position.value_pln
        total_pnl += position.pnl_pln
        table.add_row(
            position.ticker,
            _fmt_quantity(position.quantity),
            _fmt_money(position.avg_buy_price),
            _fmt_money(position.current_price),
            _fmt_money(position.value_pln),
            _fmt_signed(position.pnl_pln),
            _fmt_signed(position.pnl_pct, suffix="%"),
        )

    table.add_row(
        "RAZEM",
        "",
        "",
        "",
        _fmt_money(total_value),
        _fmt_signed(total_pnl),
        "",
        style="bold",
    )
    console.print(table)


def run() -> None:
    init_db()
    with Session(engine) as session:
        seed_assets(session)
    seed_transactions()
    with Session(engine) as session:
        fetch_and_store(session)
    render_portfolio(get_portfolio_summary())


if __name__ == "__main__":
    run()