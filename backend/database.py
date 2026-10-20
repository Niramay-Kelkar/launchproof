import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./launchproof.db")

engine = create_engine(
    DATABASE_URL,
    # timeout=30: how long a writer waits on SQLite's file lock before
    # raising "database is locked", instead of sqlite3's 5s default. Several
    # concurrent requests can legitimately write at once now (e.g. the
    # Masky-notification claim in api_runs.py), and SQLite's locking is what
    # makes that claim atomic -- a short timeout would surface as a spurious
    # 500 under load rather than the normal brief wait-then-proceed.
    connect_args={"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
