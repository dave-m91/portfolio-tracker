"""SQLite engine configuration and schema bootstrap."""

from sqlmodel import SQLModel, Session, create_engine, select

from .models import (
    DEFAULT_TARGET_ALLOCATIONS,
    TargetAllocation,
    normalize_target_weights,
)

DATABASE_URL = "sqlite:///portfolio.db"

engine = create_engine(DATABASE_URL, echo=False)


def get_session() -> Session:
    """Return a new Session bound to the application engine."""
    return Session(engine)


def seed_target_allocations() -> None:
    """Insert default target allocations when the table is empty (idempotent)."""
    session = get_session()
    try:
        if session.exec(select(TargetAllocation)).first() is not None:
            return
        for entry in normalize_target_weights(DEFAULT_TARGET_ALLOCATIONS):
            session.add(TargetAllocation(**entry))
        session.commit()
    finally:
        session.close()


def init_db() -> None:
    """Create all tables if they do not already exist, then seed defaults."""
    SQLModel.metadata.create_all(engine)
    seed_target_allocations()