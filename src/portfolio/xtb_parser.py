"""Parser mapping XTB "Cash Operations" xlsx exports onto the portfolio model.

The XTB export is a multi-sheet workbook (``Closed Positions``, ``Cash
Operations``, ``Open Positions``).  This module reads the ``Cash Operations``
sheet, whose layout is:

    row A1  "Account number" | <account id>
    row A2  "Cash Operations"
    row A3  "Date from (UTC)" | <excel serial>
    row A4  "Date to (UTC)"   | <excel serial>
    row A5  header: Type | Instrument | Ticker | Category | Time | Amount |
                    ID | Comment | Product | Position ID
    row A6+ operations, last row "Total"

Column -> model mapping
-----------------------
    Type        (A)  -> Transaction.type  ("Stock purchase" -> KUPNO,
                                           "Stock sell"     -> SPRZEDAZ)
    Instrument  (B)  -> Asset.name
    Ticker      (C)  -> Asset.ticker      (XTB "*.<X>" -> yfinance suffix)
    Category    (D)  -> Asset.asset_type  (STOCK -> AKCJA, ETF -> ETF)
    Time        (E)  -> Transaction.date  (Excel serial, UTC -> date)
    Amount      (F)  -> gross cash in the *account* currency (PLN); used to
                        reconcile, not mapped directly
    ID          (G)  -> operation id (kept for traceability / de-duplication)
    Comment     (H)  -> Transaction.quantity and Transaction.price, parsed
                        from "OPEN BUY <tkr> <legQty>/<orderQty> @ <price>"
    Product     (I)  -> account wrapper (e.g. IKE)
    Position ID (J)  -> groups the leg rows of a single position

Key structural facts discovered from the real export:

* There is **no dedicated volume or price column** -- both live inside the
  free-text ``Comment``.  ``Comment`` also carries the readable side
  (``OPEN BUY`` / ``CLOSE BUY``), which is *misleading* for sells, so the
  ``Type`` column is authoritative for the buy/sell direction.
* ``Volume`` / ``Open Price`` / ``Close Price`` / ``Commission`` columns exist
  only in the *other* two sheets (``Closed Positions`` / ``Open Positions``),
  not in ``Cash Operations``.  In this account ``Commission`` is ``0.0`` there,
  so ``Transaction.fee`` is ``0`` (see :func:`parse_cash_operations`).
* ``Amount`` for ``.WA`` instruments equals ``quantity * price`` exactly (no
  fee baked in).  For ``.DE`` instruments ``Amount`` is the PLN equivalent
  (``quantity * price * EURPLN``) while ``price`` stays in the instrument's
  currency -- hence :func:`currency_for_ticker`.
* XTB normalises GPW tickers to ``*.PL`` while :mod:`portfolio.fetcher`
  expects yfinance suffixes (``*.WA``), so tickers are rewritten on import.
"""

from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from openpyxl import load_workbook
from sqlmodel import Session, select

from .db import get_session
from .models import Asset, AssetType, Transaction, TransactionType

SHEET_NAME = "Cash Operations"

# Excel's day-zero; the 1900 leap-year bug is absorbed by using 1899-12-30.
_EXCEL_EPOCH = datetime(1899, 12, 30)

# ("Type" value -> TransactionType) for rows that represent an actual trade.
TRADE_TYPES = {
    "Stock purchase": TransactionType.KUPNO,
    "Stock sell": TransactionType.SPRZEDAZ,
}

# XTB exchange suffix -> yfinance suffix. Unmapped suffixes pass through.
_XTB_TO_YF_SUFFIX = {
    ".PL": ".WA",  # GPW (Warsaw) -- XTB writes .PL, yfinance wants .WA
    ".DE": ".DE",  # XETRA
}

_SUFFIX_TO_CURRENCY = {
    ".WA": "PLN",
    ".PL": "PLN",
    ".DE": "EUR",
}

_CATEGORY_TO_ASSET_TYPE = {
    "STOCK": AssetType.AKCJA,
    "ETF": AssetType.ETF,
    "BOND": AssetType.OBLIGACJA,
    "OBLIGATION": AssetType.OBLIGACJA,
}

# Header cells that pin down the operations table within the sheet.
_REQUIRED_HEADERS = {"Type", "Ticker", "Time", "Amount", "Comment"}


class XTBStatementError(ValueError):
    """Raised when the workbook does not look like an XTB Cash Operations."""


@dataclass(frozen=True)
class ParsedTrade:
    """A single trade leg parsed from ``Cash Operations`` (DB-agnostic)."""

    ticker: str  # normalised, yfinance-ready
    name: str
    asset_type: AssetType
    currency: str
    date: date_type
    type: TransactionType
    quantity: Decimal
    price: Decimal
    amount: Decimal  # raw signed cash from the sheet (account currency)
    transaction_fee: Decimal  # not present in the sheet -> 0
    position_id: str
    operation_id: str


@dataclass
class ParseReport:
    """Outcome of parsing a statement, incl. skipped non-trade rows."""

    trades: list[ParsedTrade] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (type, comment)

    @property
    def operation_types(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in self.trades:
            counts[trade.type.value] = counts.get(trade.type.value, 0) + 1
        for row_type, _ in self.skipped:
            counts[row_type] = counts.get(row_type, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Field-level helpers
# --------------------------------------------------------------------------- #
def excel_serial_to_datetime(value) -> datetime:
    """Convert an Excel serial date (int/float) to a naive UTC ``datetime``.

    Returns ``value`` unchanged when it already is a ``datetime``/``date``
    (openpyxl does this for date-formatted cells).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_type):
        return datetime(value.year, value.month, value.day)
    return _EXCEL_EPOCH + timedelta(days=float(value))


def normalize_ticker(xtb_ticker: str) -> str:
    """Rewrite an XTB ticker onto a yfinance-ready symbol (``.PL`` -> ``.WA``)."""
    xtb_ticker = xtb_ticker.strip().upper()
    for xtb_suffix, yf_suffix in _XTB_TO_YF_SUFFIX.items():
        if xtb_ticker.endswith(xtb_suffix):
            return xtb_ticker[: -len(xtb_suffix)] + yf_suffix
    return xtb_ticker


def currency_for_ticker(ticker: str) -> str:
    """Infer the instrument currency from its exchange suffix."""
    upper = ticker.upper()
    for suffix, currency in _SUFFIX_TO_CURRENCY.items():
        if upper.endswith(suffix):
            return currency
    return "PLN"


def asset_type_from_category(category: str) -> AssetType:
    return _CATEGORY_TO_ASSET_TYPE.get((category or "").strip().upper(), AssetType.AKCJA)


@dataclass(frozen=True)
class ParsedComment:
    """Structured view of the free-text ``Comment`` column."""

    action: str  # OPEN / CLOSE
    direction: str  # BUY / SELL (as written -- misleading for sells)
    ticker: Optional[str]  # present only on some rows
    quantity: Decimal  # this leg's quantity (the numerator)
    order_quantity: Optional[Decimal]  # whole-order quantity (denominator)
    price: Decimal


def parse_comment(comment: str) -> ParsedComment:
    """Parse ``"OPEN BUY PZU.PL 4/4.767 @ 73.42"`` into its fields.

    Tolerates the variants seen in real statements: the ticker is optional and
    the ``legQty/orderQty`` denominator is optional::

        OPEN  BUY PZU.PL 4/4.767   @ 73.42
        OPEN  BUY 7/7.2511         @ 11.580
        OPEN  BUY BFT.PL 0.0912    @ 5480.00
        OPEN  BUY 0.2988           @ 335.00
        CLOSE BUY 0.2988/0.6525    @ 336.40

    Raises ``XTBStatementError`` when the comment does not match.
    """
    import re

    pattern = re.compile(
        r"^(?P<action>OPEN|CLOSE)\s+"
        r"(?P<direction>BUY|SELL)\s+"
        r"(?:(?P<ticker>[A-Za-z][A-Za-z0-9.]*)\s+)?"  # optional, must contain a letter
        r"(?P<qty>[\d.]+)"
        r"(?:/(?P<order_qty>[\d.]+))?"
        r"\s+@\s+"
        r"(?P<price>[\d.]+)\s*$"
    )
    match = pattern.match(comment.strip())
    if not match:
        raise XTBStatementError(f"Nie mogę sparsować komentarza: {comment!r}")
    return ParsedComment(
        action=match.group("action"),
        direction=match.group("direction"),
        ticker=match.group("ticker"),
        quantity=Decimal(match.group("qty")),
        order_quantity=(
            Decimal(match.group("order_qty")) if match.group("order_qty") else None
        ),
        price=Decimal(match.group("price")),
    )


# --------------------------------------------------------------------------- #
# Sheet-level parsing
# --------------------------------------------------------------------------- #
def _read_sheet_rows(path, sheet_name: str) -> list[list]:
    # NOTE: XTB exports declare a bogus ``<dimension ref="A1"/>``, which makes
    # openpyxl's ``read_only`` iterator truncate every row to column A.  Loading
    # eagerly (small exports) recovers the real A1:J<n> range.
    workbook = load_workbook(path, data_only=True)
    if sheet_name not in workbook.sheetnames:
        raise XTBStatementError(
            f"Arkusz {sheet_name!r} nie istnieje w {path}. "
            f"Dostępne arkusze: {workbook.sheetnames}"
        )
    sheet = workbook[sheet_name]
    # ``values_only`` yields tuples of cell values; trailing empties are trimmed.
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def _locate_header(rows: Iterable[list]) -> tuple[int, list[str]]:
    """Return ``(header_index, headers)`` for the operations table header row."""
    for index, row in enumerate(rows):
        cells = {(cell or "").strip() if isinstance(cell, str) else cell for cell in row}
        if _REQUIRED_HEADERS.issubset(cells):
            return index, [str(cell).strip() if cell is not None else "" for cell in row]
    raise XTBStatementError("Nie znalazłem nagłówka operacji w arkuszu.")


def parse_cash_operations(path) -> ParseReport:
    """Parse the ``Cash Operations`` sheet into :class:`ParsedTrade` objects.

    Non-trade cash movements (``IKE deposit``, ``Free funds interest``, the
    trailing ``Total`` row) are collected in :attr:`ParseReport.skipped`
    instead of being mapped to a ``Transaction``.
    """
    rows = _read_sheet_rows(path, SHEET_NAME)
    header_index, headers = _locate_header(rows)
    report = ParseReport()

    for raw in rows[header_index + 1 :]:
        record = dict(zip(headers, raw))
        row_type = (record.get("Type") or "").strip() if isinstance(
            record.get("Type"), str
        ) else record.get("Type")

        if not row_type or row_type == "Total":
            continue

        if row_type not in TRADE_TYPES:
            report.skipped.append((row_type, str(record.get("Comment") or "")))
            continue

        comment = record.get("Comment")
        if not comment:
            report.skipped.append((row_type, ""))
            continue

        parsed = parse_comment(str(comment))
        ticker = normalize_ticker(str(record["Ticker"]))
        report.trades.append(
            ParsedTrade(
                ticker=ticker,
                name=str(record.get("Instrument") or ticker),
                asset_type=asset_type_from_category(record.get("Category")),
                currency=currency_for_ticker(ticker),
                date=excel_serial_to_datetime(record["Time"]).date(),
                type=TRADE_TYPES[row_type],
                quantity=parsed.quantity,
                price=parsed.price,
                amount=Decimal(str(record.get("Amount") or "0")),
                transaction_fee=Decimal("0"),
                position_id=str(record.get("Position ID") or ""),
                operation_id=str(record.get("ID") or ""),
            )
        )
    return report


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
@dataclass
class ImportReport:
    """What an import run did."""

    assets_created: list[str] = field(default_factory=list)
    transactions_added: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


def import_cash_operations(path, session: Optional[Session] = None) -> ImportReport:
    """Parse an XTB statement and persist it through the portfolio model.

    Missing :class:`Asset` rows are created automatically (name/type/currency
    taken from the statement).  Each trade *leg* becomes one
    :class:`Transaction`, so the two legs XTB emits for a single order are
    stored faithfully and sum to the full position.

    The import is **not** de-duplicated across runs -- a statement re-imported
    into the same DB duplicates rows.  ``operation_id`` is preserved on the
    parsed trade so a caller can add de-duplication if needed.
    """
    report = parse_cash_operations(path)

    owns_session = session is None
    session = session or get_session()
    try:
        known = {asset.ticker: asset for asset in session.exec(select(Asset)).all()}
        result = ImportReport(skipped=report.skipped)

        for trade in report.trades:
            asset = known.get(trade.ticker)
            if asset is None:
                asset = Asset(
                    ticker=trade.ticker,
                    name=trade.name,
                    asset_type=trade.asset_type,
                    currency=trade.currency,
                )
                session.add(asset)
                session.flush()  # assign asset.id
                known[trade.ticker] = asset
                result.assets_created.append(trade.ticker)

            session.add(
                Transaction(
                    asset_id=asset.id,
                    date=trade.date,
                    type=trade.type,
                    quantity=trade.quantity,
                    price=trade.price,
                    fee=trade.transaction_fee,
                )
            )
            result.transactions_added += 1

        session.commit()
        return result
    finally:
        if owns_session:
            session.close()


__all__ = [
    "ParsedTrade",
    "ParsedComment",
    "ParseReport",
    "ImportReport",
    "XTBStatementError",
    "parse_comment",
    "parse_cash_operations",
    "import_cash_operations",
    "normalize_ticker",
    "currency_for_ticker",
    "asset_type_from_category",
    "excel_serial_to_datetime",
]
