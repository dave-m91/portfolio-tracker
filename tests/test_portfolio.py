"""Unit tests for portfolio transaction recording and P&L computation."""

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from portfolio import portfolio
from portfolio.models import (
    Asset,
    AssetType,
    PriceHistory,
    Transaction,
    TransactionType,
)


@pytest.fixture
def portfolio_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the portfolio module at an isolated SQLite database."""
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(test_engine)

    def factory() -> Session:
        return Session(test_engine)

    monkeypatch.setattr(portfolio, "get_session", factory)
    return test_engine


def _add_asset(
    ticker: str,
    name: str,
    asset_type: AssetType,
    currency: str,
) -> int:
    session = portfolio.get_session()
    asset = Asset(
        ticker=ticker, name=name, asset_type=asset_type, currency=currency
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)
    asset_id = asset.id
    session.close()
    assert asset_id is not None
    return asset_id


def _add_price(asset_id: int, close_price: Decimal, price_date: date) -> None:
    session = portfolio.get_session()
    session.add(
        PriceHistory(asset_id=asset_id, date=price_date, close_price=close_price)
    )
    session.commit()
    session.close()


def _summary_for(summaries, ticker: str):
    return next(s for s in summaries if s.ticker == ticker)


def test_record_transaction_adds_row(portfolio_db) -> None:
    asset_id = _add_asset("CDR.WA", "CD Projekt", AssetType.AKCJA, "PLN")
    portfolio.record_transaction(
        "CDR.WA",
        date(2026, 1, 10),
        TransactionType.KUPNO,
        Decimal("50"),
        Decimal("180.00"),
        Decimal("5"),
    )

    session = portfolio.get_session()
    rows = session.exec(select(Transaction)).all()
    session.close()
    assert len(rows) == 1
    assert rows[0].asset_id == asset_id
    assert rows[0].type is TransactionType.KUPNO
    assert rows[0].quantity == Decimal("50")
    assert rows[0].price == Decimal("180.00")
    assert rows[0].fee == Decimal("5")


def test_record_transaction_unknown_ticker_raises(portfolio_db) -> None:
    with pytest.raises(ValueError):
        portfolio.record_transaction(
            "NOPE.WA",
            date(2026, 1, 10),
            TransactionType.KUPNO,
            Decimal("1"),
            Decimal("10"),
        )


def test_pln_position_pnl_and_no_nbp_fetch(portfolio_db) -> None:
    asset_id = _add_asset("CDR.WA", "CD Projekt", AssetType.AKCJA, "PLN")
    portfolio.record_transaction(
        "CDR.WA",
        date(2026, 1, 10),
        TransactionType.KUPNO,
        Decimal("50"),
        Decimal("180.00"),
        Decimal("5"),
    )
    _add_price(asset_id, Decimal("200.00"), date(2026, 1, 10))

    # No EUR asset -> default rate must not trigger an NBP call.
    summaries = portfolio.get_portfolio_summary()
    position = _summary_for(summaries, "CDR.WA")

    assert position.currency == "PLN"
    assert position.quantity == Decimal("50")
    assert position.avg_buy_price == Decimal("180.10")
    assert position.current_price == Decimal("200.00")
    assert position.value_pln == Decimal("10000.00")
    assert position.pnl_pln == Decimal("995.00")
    assert position.pnl_pct == Decimal("11.05")


def test_eur_position_pnl_converted_to_pln(portfolio_db) -> None:
    asset_id = _add_asset(
        "VWCE.DE", "Vanguard FTSE All-World UCITS ETF", AssetType.ETF, "EUR"
    )
    portfolio.record_transaction(
        "VWCE.DE",
        date(2026, 1, 10),
        TransactionType.KUPNO,
        Decimal("10"),
        Decimal("130.00"),
        Decimal("2"),
    )
    _add_price(asset_id, Decimal("140.00"), date(2026, 1, 10))

    summaries = portfolio.get_portfolio_summary(eur_pln_rate=Decimal("4.5"))
    position = _summary_for(summaries, "VWCE.DE")

    assert position.currency == "EUR"
    assert position.quantity == Decimal("10")
    assert position.avg_buy_price == Decimal("130.20")
    assert position.current_price == Decimal("140.00")
    assert position.value_local == Decimal("1400.00")
    assert position.value_pln == Decimal("6300.00")  # 1400 * 4.5
    assert position.pnl_local == Decimal("98.00")
    assert position.pnl_pln == Decimal("441.00")  # 98 * 4.5
    assert position.pnl_pct == Decimal("7.53")


def test_quantity_is_buy_minus_sell(portfolio_db) -> None:
    asset_id = _add_asset("CDR.WA", "CD Projekt", AssetType.AKCJA, "PLN")
    portfolio.record_transaction(
        "CDR.WA",
        date(2026, 1, 10),
        TransactionType.KUPNO,
        Decimal("50"),
        Decimal("180.00"),
        Decimal("5"),
    )
    portfolio.record_transaction(
        "CDR.WA",
        date(2026, 1, 20),
        TransactionType.SPRZEDAZ,
        Decimal("20"),
        Decimal("200.00"),
        Decimal("2"),
    )
    _add_price(asset_id, Decimal("210.00"), date(2026, 1, 20))

    summaries = portfolio.get_portfolio_summary()
    position = _summary_for(summaries, "CDR.WA")

    # Average buy price ignores sold units and their realized P&L.
    assert position.quantity == Decimal("30")
    assert position.avg_buy_price == Decimal("180.10")
    # 30 units @ 210 vs break-even 180.10
    assert position.value_pln == Decimal("6300.00")
    assert position.pnl_pln == Decimal("897.00")
    assert position.pnl_pct == Decimal("16.60")