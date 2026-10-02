"""Streamlit dashboard for the portfolio tracker.

Run with: ``streamlit run src/portfolio/app.py``
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlmodel import select

# When Streamlit runs this file directly, the script's own directory lands first
# on ``sys.path``; because it also contains a ``portfolio.py`` module, that would
# shadow the ``portfolio`` package and break the absolute imports below. Put
# ``src/`` at the front of ``sys.path`` (deduplicating) so imports resolve to the
# installed package.
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
sys.path = [_SRC_DIR] + [p for p in sys.path if p != _SRC_DIR]

from portfolio.db import get_session, init_db
from portfolio.models import Asset, TransactionType
from portfolio.portfolio import PositionSummary, get_portfolio_summary, record_transaction

st.set_page_config(page_title="Portfolio Tracker", page_icon="📈", layout="wide")


def _tickers() -> list[str]:
    """Return all tracked tickers, sorted, for the transaction form."""
    session = get_session()
    try:
        assets = session.exec(select(Asset).order_by(Asset.ticker)).all()
        return [asset.ticker for asset in assets]
    finally:
        session.close()


def _summary_frame(summary: list[PositionSummary]) -> pd.DataFrame:
    """Convert position summaries into a numeric DataFrame for plots/table."""
    rows = [
        {
            "Ticker": s.ticker,
            "Nazwa": s.name,
            "Klasa": s.asset_type.value,
            "Waluta": s.currency,
            "Ilość": float(s.quantity),
            "Śr. cena zakupu": float(s.avg_buy_price),
            "Aktualny kurs": float(s.current_price),
            "Wartość (PLN)": float(s.value_pln),
            "P&L (PLN)": float(s.pnl_pln),
            "P&L (%)": float(s.pnl_pct),
        }
        for s in summary
    ]
    return pd.DataFrame(rows)


def _render_metrics(summary: list[PositionSummary]) -> None:
    total_value = sum((s.value_pln for s in summary), Decimal("0"))
    total_pnl = sum((s.pnl_pln for s in summary), Decimal("0"))
    total_cost = sum((s.value_pln - s.pnl_pln for s in summary), Decimal("0"))
    total_return = (total_pnl / total_cost * 100) if total_cost else Decimal("0")

    left, middle, right = st.columns(3)
    left.metric("Wartość portfela (PLN)", f"{float(total_value):,.2f}")
    middle.metric("Całkowity P&L (PLN)", f"{float(total_pnl):,.2f}")
    right.metric("Zwrot (%)", f"{float(total_return):.2f}%")


def _render_charts(df: pd.DataFrame) -> None:
    left, right = st.columns(2)
    with left:
        by_class = df.groupby("Klasa", as_index=False)["Wartość (PLN)"].sum()
        fig_class = px.pie(
            by_class,
            names="Klasa",
            values="Wartość (PLN)",
            title="Podział według klas aktywów",
        )
        st.plotly_chart(fig_class, width="stretch")
    with right:
        fig_holding = px.pie(
            df,
            names="Ticker",
            values="Wartość (PLN)",
            title="Udział poszczególnych walorów",
        )
        st.plotly_chart(fig_holding, width="stretch")


def _render_table(df: pd.DataFrame) -> None:
    st.subheader("Pozycje")
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker"),
            "Nazwa": st.column_config.TextColumn("Nazwa"),
            "Klasa": st.column_config.TextColumn("Klasa"),
            "Waluta": st.column_config.TextColumn("Waluta"),
            "Ilość": st.column_config.NumberColumn("Ilość", format="%.4f"),
            "Śr. cena zakupu": st.column_config.NumberColumn(
                "Śr. cena zakupu", format="%.2f"
            ),
            "Aktualny kurs": st.column_config.NumberColumn(
                "Aktualny kurs", format="%.2f"
            ),
            "Wartość (PLN)": st.column_config.NumberColumn(
                "Wartość (PLN)", format="%.2f PLN"
            ),
            "P&L (PLN)": st.column_config.NumberColumn(
                "P&L (PLN)", format="%.2f PLN"
            ),
            "P&L (%)": st.column_config.NumberColumn("P&L (%)", format="%.2f%%"),
        },
    )


def _render_transaction_form() -> None:
    st.sidebar.header("Dodaj transakcję")
    tickers = _tickers()
    if not tickers:
        st.sidebar.info(
            "Brak aktywów w bazie. Uruchom najpierw `python -m portfolio.main`."
        )
        return

    with st.sidebar.form("add_transaction"):
        ticker = st.selectbox("Ticker", tickers)
        tx_date = st.date_input("Data", value=date.today())
        tx_type = st.selectbox(
            "Typ",
            [TransactionType.KUPNO, TransactionType.SPRZEDAZ],
            format_func=lambda t: t.value,
        )
        quantity = st.number_input("Ilość", min_value=0.0, step=1.0, format="%.4f")
        price = st.number_input("Cena", min_value=0.0, step=0.01, format="%.2f")
        fee = st.number_input(
            "Prowizja", min_value=0.0, step=0.01, format="%.2f", value=0.0
        )
        submitted = st.form_submit_button("Zapisz transakcję")

    if submitted:
        record_transaction(
            ticker,
            tx_date,
            tx_type,
            Decimal(str(quantity)),
            Decimal(str(price)),
            Decimal(str(fee)),
        )
        st.rerun()


def main() -> None:
    init_db()
    st.title("📈 Portfolio Tracker")

    try:
        summary = get_portfolio_summary()
    except RuntimeError as exc:
        st.error(str(exc))
        return

    if not summary:
        st.info(
            "Brak pozycji. Uruchom `python -m portfolio.main`, aby zainicjalizować "
            "bazę, lub dodaj transakcję w panelu bocznym."
        )
        _render_transaction_form()
        return

    df = _summary_frame(summary)
    _render_metrics(summary)
    _render_charts(df)
    _render_table(df)
    _render_transaction_form()


if __name__ == "__main__":
    main()