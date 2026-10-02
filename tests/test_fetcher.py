"""Unit tests for the yfinance-backed price fetcher."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from portfolio import fetcher
from portfolio.models import AssetType


class _FakeTicker:
    """Stub replacing ``yf.Ticker`` with a canned history frame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def history(self, period: str) -> pd.DataFrame:
        return self._frame


@pytest.fixture
def two_day_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-30")]
    )
    return pd.DataFrame({"Close": [177.0, 178.5]}, index=idx)


def test_latest_close_returns_prior_trading_day(
    monkeypatch: pytest.MonkeyPatch, two_day_frame: pd.DataFrame
) -> None:
    monkeypatch.setattr(
        fetcher.yf, "Ticker", lambda ticker: _FakeTicker(two_day_frame)
    )
    result = fetcher.fetch_yesterday_close("CDR.WA", as_of=date(2026, 9, 30))
    assert result == (date(2026, 9, 29), Decimal("177.0"))


def test_today_row_is_excluded(
    monkeypatch: pytest.MonkeyPatch, two_day_frame: pd.DataFrame
) -> None:
    # Only the row dated 2026-09-30 exists; with as_of 2026-09-30 nothing is
    # strictly before it, so the fetcher must return None.
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-30")])
    frame = pd.DataFrame({"Close": [178.5]}, index=idx)
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda ticker: _FakeTicker(frame))
    assert fetcher.fetch_yesterday_close("VWCE.DE", as_of=date(2026, 9, 30)) is None


def test_empty_history_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetcher.yf, "Ticker", lambda ticker: _FakeTicker(pd.DataFrame())
    )
    assert fetcher.fetch_yesterday_close("VWCE.DE") is None


def test_resolve_ticker_passes_suffix_through() -> None:
    assert fetcher.resolve_ticker("CDR.WA") == "CDR.WA"
    assert fetcher.resolve_ticker("VWCE.DE") == "VWCE.DE"


def test_default_market_suffix_by_asset_type() -> None:
    assert fetcher.default_market_suffix(AssetType.AKCJA) == ".WA"
    assert fetcher.default_market_suffix(AssetType.ETF) == ".DE"
    assert fetcher.default_market_suffix(AssetType.OBLIGACJA) == ""