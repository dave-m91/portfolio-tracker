"""Market data fetching via yfinance.

Supported ticker conventions:
- GPW (Warsaw Stock Exchange): ``CDR.WA``
- European ETFs (XETRA etc.): ``VWCE.DE``
"""

import json
import urllib.request
from datetime import date
from decimal import Decimal
from typing import Optional

import yfinance as yf

from .models import AssetType

# NBP exchange-rate API, table A (mid rates).
NBP_EUR_PLN_URL = "https://api.nbp.pl/api/exchangerates/rates/a/eur/?format=json"

# Enough calendar history to always contain the previous trading session,
# even right after a weekend or a multi-day market closure.
_HISTORY_PERIOD = "1mo"


def resolve_ticker(ticker: str) -> str:
    """Return the yfinance-ready ticker for a raw instrument ticker.

    The ``.WA`` (GPW) and ``.DE`` (XETRA/European) suffixes are conventional
    yfinance exchange suffixes that are passed through unchanged.
    """
    return ticker


def _latest_close(ticker: str, as_of: date) -> Optional[tuple[date, Decimal]]:
    """Return the latest ``(date, close)`` strictly before ``as_of``."""
    frame = yf.Ticker(ticker).history(period=_HISTORY_PERIOD)
    if frame is None or frame.empty:
        return None
    past = frame[frame.index.date < as_of]
    if past.empty:
        return None
    last = past.iloc[-1]
    close = last["Close"]
    if close is None or not isinstance(close, (int, float)):
        return None
    return last.name.date(), Decimal(str(round(float(close), 4)))


def fetch_yesterday_close(
    ticker: str, as_of: Optional[date] = None
) -> Optional[tuple[date, Decimal]]:
    """Fetch the last close before ``as_of`` (default: today).

    Returns a ``(date, close_price)`` tuple, or ``None`` on failure.
    """
    return _latest_close(resolve_ticker(ticker), as_of or date.today())


def fetch_eur_pln_rate() -> Optional[Decimal]:
    """Fetch the latest average EUR/PLN mid rate from the NBP table A API.

    Returns the rounded mid rate as a ``Decimal``, or ``None`` on any failure
    (network error, unexpected payload). The response shape is::

        {"code": "EUR", "rates": [{"mid": 4.377, "effectiveDate": "..."}]}
    """
    try:
        with urllib.request.urlopen(NBP_EUR_PLN_URL, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        mid = payload["rates"][0]["mid"]
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return None
    try:
        return Decimal(str(mid))
    except (ValueError, TypeError):
        return None


SUPPORTED_EXCHANGE_SUFFIXES = frozenset({".WA", ".DE"})


def default_market_suffix(asset_type: AssetType) -> str:
    """Suggest a yfinance exchange suffix for a given asset type.

    GPW equities use ``.WA``; European ETFs use ``.DE``. Obligations have no
    canonical yfinance suffix.
    """
    if asset_type is AssetType.ETF:
        return ".DE"
    if asset_type is AssetType.AKCJA:
        return ".WA"
    return ""