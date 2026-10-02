"""SQLite engine configuration and schema bootstrap."""

from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = "sqlite:///portfolio.db"

engine = create_engine(DATABASE_URL, echo=False)


def get_session() -> Session:
    """Return a new Session bound to the application engine."""
    return Session(engine)


def init_db() -> None:
    """Create all tables if they do not already exist."""
    SQLModel.metadata.create_all(engine)