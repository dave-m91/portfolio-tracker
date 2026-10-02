"""Database table definitions backed by SQLModel / SQLAlchemy."""

from datetime import date as date_type
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class AssetType(str, Enum):
    """Instrument class. Stored textually in the DB for readability."""

    AKCJA = "AKCJA"
    ETF = "ETF"
    OBLIGACJA = "OBLIGACJA"


class TransactionType(str, Enum):
    """Trade direction."""

    KUPNO = "KUPNO"
    SPRZEDAZ = "SPRZEDAŻ"


class Asset(SQLModel, table=True):
    """A tradable instrument tracked by the portfolio."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, unique=True)
    name: str
    asset_type: AssetType
    currency: str = Field(default="PLN")


class Transaction(SQLModel, table=True):
    """A single buy/sell event for an asset."""

    id: Optional[int] = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    date: date_type
    type: TransactionType
    quantity: Decimal = Field(default=Decimal("0"))
    price: Decimal = Field(default=Decimal("0"))
    fee: Decimal = Field(default=Decimal("0"))


class PriceHistory(SQLModel, table=True):
    """Historized daily close price for an asset."""

    id: Optional[int] = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    date: date_type = Field(index=True)
    close_price: Decimal = Field(default=Decimal("0"))