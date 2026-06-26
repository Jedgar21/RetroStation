"""
app/database.py — SQLite engine + session helpers.

Single-file SQLite at data/fieldstation.db. WAL mode so the streaming engine can
read the schedule while the API writes to it without lock contention.
"""

from pathlib import Path
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import event

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fieldstation.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},  # FastAPI + background tasks
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")       # concurrent read/write
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db():
    # import models so SQLModel registers the tables before create_all
    from app import models  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session
