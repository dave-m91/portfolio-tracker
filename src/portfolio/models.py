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


class AllocationCategory(str, Enum):
    """Coarse bucket used by the target-allocation plan."""

    ETF = "ETF"
    AKCJA = "AKCJA"


class TargetAllocation(SQLModel, table=True):
    """Target portfolio weight for a canonical ticker."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, unique=True)
    name: str
    category: AllocationCategory
    target_weight: Decimal = Field(default=Decimal("0"))
    is_fixed: bool = Field(default=False)


# Default target weights; DIG.WA closes the sum to exactly 100%.
DEFAULT_TARGET_ALLOCATIONS: list[dict] = [
    {"ticker": "QDVE.DE", "name": "S&P 500 IT Sector", "category": AllocationCategory.ETF, "target_weight": Decimal("35.0"), "is_fixed": True},
    {"ticker": "IUES.DE", "name": "S&P 500 Energy Sector", "category": AllocationCategory.ETF, "target_weight": Decimal("10.0"), "is_fixed": True},
    {"ticker": "CDR.WA", "name": "CD Projekt", "category": AllocationCategory.AKCJA, "target_weight": Decimal("10.0"), "is_fixed": False},
    {"ticker": "NWG.WA", "name": "Newag", "category": AllocationCategory.AKCJA, "target_weight": Decimal("10.0"), "is_fixed": False},
    {"ticker": "BFT.WA", "name": "Benefit Systems", "category": AllocationCategory.AKCJA, "target_weight": Decimal("8.0"), "is_fixed": False},
    {"ticker": "SNT.WA", "name": "Synektik", "category": AllocationCategory.AKCJA, "target_weight": Decimal("8.0"), "is_fixed": False},
    {"ticker": "DNP.WA", "name": "Dino Polska", "category": AllocationCategory.AKCJA, "target_weight": Decimal("8.0"), "is_fixed": False},
    {"ticker": "PZU.WA", "name": "PZU", "category": AllocationCategory.AKCJA, "target_weight": Decimal("8.0"), "is_fixed": False},
    {"ticker": "DIG.WA", "name": "Digital Network", "category": AllocationCategory.AKCJA, "target_weight": Decimal("3.0"), "is_fixed": False},
]


# Actual XTB ticker -> canonical target-allocation ticker it belongs to.
TARGET_TICKER_ALIASES: dict[str, str] = {
    "QDVF.DE": "IUES.DE",  # XTB writes the Energy ETF as QDVF.DE
}


def normalize_target_weights(entries: list[dict]) -> list[dict]:
    """Return ``entries`` with ``target_weight`` scaled to sum to 100%."""
    total = sum((entry["target_weight"] for entry in entries), Decimal("0"))
    if total == Decimal("100") or total == 0:
        return [dict(entry) for entry in entries]
    factor = Decimal("100") / total
    return [
        {**entry, "target_weight": entry["target_weight"] * factor}
        for entry in entries
    ]