"""
Tiny, targeted schema migrations for columns `Base.metadata.create_all()`
cannot add to a table that already exists -- it only creates missing
tables, never alters existing ones. There is no migration framework
(Alembic etc.) anywhere in this project to extend, so this adds the
smallest mechanism that covers exactly what commit 20f96f4 needs, not a
general-purpose migration system: a short, explicit list of (table,
column, type) entries, each applied only if the column is actually
missing, checked via introspection first.

Safety properties this relies on:
- Idempotent: every entry is a check-then-ALTER, so calling run_migrations()
  on every app startup (including repeated or concurrent startups) is
  always safe -- it never re-applies an ALTER that already landed.
- Non-destructive: only ever ADDs a nullable column; existing rows and
  their data are never read, rewritten, or dropped.
- Concurrency-safe: if two processes start at the same moment and both
  pass the "missing" check before either ALTERs, SQLite's single-writer
  lock serializes the two ALTER TABLE statements; whichever runs second
  gets "duplicate column name", which is caught and treated as success
  (another process already did the work), not an error.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

logger = logging.getLogger("launchproof.migrations")

# (table, column, SQL type) -- every column this project has ever added to
# an already-shipped table via `ALTER TABLE ... ADD COLUMN`, in order.
# Append here, never edit or remove a past entry, the same discipline a
# real migration-history table would enforce.
_COLUMN_MIGRATIONS = [
    ("masky_runs", "masky_notify_status", "VARCHAR"),
    ("masky_runs", "masky_conversation_id", "VARCHAR"),
]


def _add_column_if_missing(engine: Engine, table: str, column: str, sql_type: str) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        # Table doesn't exist at all yet -- create_all() (run right before
        # this) creates it fresh with every current column already in
        # place, so there is nothing for this migration to do.
        return
    existing_columns = {col["name"] for col in inspector.get_columns(table)}
    if column in existing_columns:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        logger.info("migrations: added %s.%s", table, column)
    except OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info("migrations: %s.%s already added by a concurrent process", table, column)
            return
        raise


def run_migrations(engine: Engine) -> None:
    """Call once at startup, immediately after Base.metadata.create_all().
    Safe to call every time the process starts, including when two
    instances start at once -- see module docstring."""
    for table, column, sql_type in _COLUMN_MIGRATIONS:
        _add_column_if_missing(engine, table, column, sql_type)


if __name__ == "__main__":
    # Manual alternative to starting the app: `python migrations.py`
    # upgrades launchproof.db (or whatever DATABASE_URL points at) in
    # place, without importing main.py or starting a server.
    from database import engine as _engine
    from database import Base as _Base
    _Base.metadata.create_all(bind=_engine)
    run_migrations(_engine)
    print("Migrations applied (or already up to date).")
