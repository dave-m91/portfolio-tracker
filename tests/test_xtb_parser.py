"""Tests for the XTB Cash Operations parser."""

from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from portfolio.models import AssetType, TransactionType
from portfolio.xtb_parser import (
    ParsedComment,
    XTBStatementError,
    currency_for_ticker,
    excel_serial_to_datetime,
    import_cash_operations,
    normalize_ticker,
    parse_cash_operations,
    parse_comment,
)


def _write_statement(path) -> None:
    """Build a miniature XTB-style workbook mirroring the real layout."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cash Operations"
    sheet.append(["Account number", "12345678"])
    sheet.append(["Cash Operations", ""])
    sheet.append(["Date from (UTC)", "38718.0"])
    sheet.append(["Date to (UTC)", "46308.749"])
    sheet.append(
        [
            "Type",
            "Instrument",
            "Ticker",
            "Category",
            "Time",
            "Amount",
            "ID",
            "Comment",
            "Product",
            "Position ID",
        ]
    )
    # GPW buy, split into two legs
    sheet.append(
        ["Stock purchase", "PZU", "PZU.PL", "STOCK", "46308.4072", "-293.68",
         "1477031643", "OPEN BUY PZU.PL 4/4.767 @ 73.42", "IKE", "2870393585"]
    )
    sheet.append(
        ["Stock purchase", "PZU", "PZU.PL", "STOCK", "46308.4072", "-56.31",
         "1477031642", "OPEN BUY PZU.PL 0.767/4.767 @ 73.42", "IKE", "2870393585"]
    )
    # XETRA ETF buy leg with no ticker in the comment
    sheet.append(
        ["Stock purchase", "S&P 500 IT Sector", "QDVE.DE", "ETF", "46266.2613",
         "-579.72", "1418956044", "OPEN BUY 3/3.1049 @ 44.410", "IKE", "2788614480"]
    )
    # sell -- "CLOSE BUY" wording, direction comes from the Type column
    sheet.append(
        ["Stock sell", "XTB", "XTB.PL", "STOCK", "46295.3467", "153.4",
         "1458051445", "CLOSE BUY XTB.PL 1 @ 153.40", "IKE", "2775601308"]
    )
    # non-trade cash movements -> skipped
    sheet.append(
        ["IKE deposit", "", "", "", "46308.403", "4000.0", "1477030707",
         "Transfer in operation on account with id 12345678", "IKE", ""]
    )
    sheet.append(
        ["Free funds interest", "", "", "", "46298.611", "0.08", "1465697125",
         "Free-funds Interest 2026-09", "IKE", ""]
    )
    sheet.append(["Total", None, None, None, None, "7.54"])
    workbook.save(path)


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #
def test_normalize_ticker_rewrites_pl_to_wa():
    assert normalize_ticker("PZU.PL") == "PZU.WA"
    assert normalize_ticker("QDVF.DE") == "QDVF.DE"
    assert normalize_ticker("cdr.pl") == "CDR.WA"


def test_currency_for_ticker():
    assert currency_for_ticker("PZU.WA") == "PLN"
    assert currency_for_ticker("QDVF.DE") == "EUR"


def test_excel_serial_to_datetime():
    assert excel_serial_to_datetime("46308.749").date() == date(2026, 10, 13)
    assert excel_serial_to_datetime("38718.0").date() == date(2006, 1, 1)


@pytest.mark.parametrize(
    "comment, qty, order_qty, price",
    [
        ("OPEN BUY PZU.PL 4/4.767 @ 73.42", "4", "4.767", "73.42"),
        ("OPEN BUY 7/7.2511 @ 11.580", "7", "7.2511", "11.580"),
        ("OPEN BUY BFT.PL 0.0912 @ 5480.00", "0.0912", None, "5480.00"),
        ("OPEN BUY 0.2988 @ 335.00", "0.2988", None, "335.00"),
        ("CLOSE BUY 0.2988/0.6525 @ 336.40", "0.2988", "0.6525", "336.40"),
    ],
)
def test_parse_comment_variants(comment, qty, order_qty, price):
    parsed = parse_comment(comment)
    assert parsed.quantity == Decimal(qty)
    assert parsed.order_quantity == (Decimal(order_qty) if order_qty else None)
    assert parsed.price == Decimal(price)


def test_parse_comment_ticker_optional_and_not_numeric():
    assert parse_comment("OPEN BUY PZU.PL 4/4.767 @ 73.42").ticker == "PZU.PL"
    # a bare numeric leg must not be mistaken for a ticker
    assert parse_comment("OPEN BUY 7/7.2511 @ 11.580").ticker is None


def test_parse_comment_rejects_garbage():
    with pytest.raises(XTBStatementError):
        parse_comment("Transfer in operation on account with id 12345678")


# --------------------------------------------------------------------------- #
# Sheet parsing
# --------------------------------------------------------------------------- #
def test_parse_cash_operations(tmp_path):
    path = tmp_path / "stmt.xlsx"
    _write_statement(path)
    report = parse_cash_operations(path)

    assert len(report.trades) == 4
    # non-trade rows are skipped, not mapped
    assert {t for t, _ in report.skipped} == {"IKE deposit", "Free funds interest"}

    pzu = [t for t in report.trades if t.ticker == "PZU.WA"]
    assert len(pzu) == 2
    assert all(t.type is TransactionType.KUPNO for t in pzu)
    assert all(t.asset_type is AssetType.AKCJA for t in pzu)
    assert all(t.currency == "PLN" for t in pzu)
    assert pzu[0].date == date(2026, 10, 13)
    assert sum(t.quantity for t in pzu) == Decimal("4.767")
    assert pzu[0].position_id == "2870393585"

    etf = next(t for t in report.trades if t.ticker == "QDVE.DE")
    assert etf.asset_type is AssetType.ETF
    assert etf.currency == "EUR"
    assert etf.price == Decimal("44.410")

    sell = next(t for t in report.trades if t.type is TransactionType.SPRZEDAZ)
    assert sell.ticker == "XTB.WA"
    assert sell.price == Decimal("153.40")

    # fee is absent from the sheet
    assert all(t.transaction_fee == Decimal("0") for t in report.trades)


def test_import_creates_assets_and_transactions(tmp_path):
    from sqlmodel import SQLModel, Session, create_engine, select

    from portfolio.models import Asset, Transaction

    path = tmp_path / "stmt.xlsx"
    _write_statement(path)

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        result = import_cash_operations(path, session=session)

    assert result.transactions_added == 4
    assert set(result.assets_created) == {"PZU.WA", "QDVE.DE", "XTB.WA"}
    with Session(engine) as session:
        assert {a.ticker for a in session.exec(select(Asset)).all()} == {
            "PZU.WA", "QDVE.DE", "XTB.WA",
        }
        assert len(session.exec(select(Transaction)).all()) == 4


# --------------------------------------------------------------------------- #
# Integration against a real XTB export (skipped when absent)
# --------------------------------------------------------------------------- #
def _real_statement():
    from pathlib import Path

    candidates = sorted(Path("data").glob("*.xlsx"))
    return candidates[0] if candidates else None


@pytest.mark.skipif(_real_statement() is None, reason="brak pliku w data/")
def test_real_export_parses_fully():
    report = parse_cash_operations(_real_statement())
    # the export carries a bogus <dimension ref="A1"/>; make sure we still see
    # the full-width rows (regression guard for the read_only truncation bug)
    assert len(report.trades) == 40
    assert {t.type for t in report.trades} == {
        TransactionType.KUPNO,
        TransactionType.SPRZEDAZ,
    }
    assert all(t.ticker.endswith((".WA", ".DE")) for t in report.trades)
    assert {(typ) for typ, _ in report.skipped} == {
        "IKE deposit",
        "Free funds interest",
    }
