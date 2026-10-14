"""CLI entry point for the portfolio tracker.

Run with: ``python -m portfolio.main``
"""

from decimal import Decimal

from rich.console import Console
from rich.table import Table
from sqlmodel import Session

from .db import engine, init_db
from .portfolio import (
    PositionSummary,
    get_portfolio_summary,
    refresh_prices,
)

console = Console()


def fetch_and_store(session: Session) -> None:
    """Fetch latest closes for every asset and persist them."""
    report = refresh_prices(session=session)
    for ticker in report.failed:
        console.print(
            f"[yellow]Brak danych dla {ticker}[/yellow]",
            highlight=False,
        )


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
        fetch_and_store(session)
    render_portfolio(get_portfolio_summary())


if __name__ == "__main__":
    run()