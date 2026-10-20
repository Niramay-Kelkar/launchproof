"""
Tests for migrations.py against both a fresh database and one built with the
exact pre-20f96f4 masky_runs schema (no masky_notify_status /
masky_conversation_id columns) -- the schema that previously made
POST /api/runs crash with "no such column" once those fields were added to
the model. Each test builds its own temp SQLite file; nothing here touches
backend/launchproof.db.
"""
import sqlite3

from sqlalchemy import create_engine, inspect, text

import migrations

_PRE_20F96F4_MASKY_RUNS_SCHEMA = """
CREATE TABLE masky_runs (
    run_id VARCHAR NOT NULL PRIMARY KEY,
    idempotency_key VARCHAR,
    idea_text VARCHAR NOT NULL,
    target_customer VARCHAR,
    requested_action VARCHAR,
    mode VARCHAR NOT NULL,
    error VARCHAR,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def _build_pre_migration_db(path, with_existing_row=True):
    con = sqlite3.connect(path)
    con.execute(_PRE_20F96F4_MASKY_RUNS_SCHEMA)
    if with_existing_row:
        con.execute(
            "INSERT INTO masky_runs (run_id, idea_text, mode) VALUES (?, ?, ?)",
            ("lp_preexisting", "a pre-existing idea from before the migration", "demo"),
        )
    con.commit()
    con.close()


# ---------- 6. existing-schema migration preserves data and allows API use ----------

def test_migration_adds_missing_columns_to_a_pre_existing_database(tmp_path):
    db_path = tmp_path / "pre_migration.db"
    _build_pre_migration_db(db_path)

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    migrations.run_migrations(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("masky_runs")}
    assert "masky_notify_status" in columns
    assert "masky_conversation_id" in columns


def test_migration_preserves_existing_rows(tmp_path):
    db_path = tmp_path / "pre_migration.db"
    _build_pre_migration_db(db_path)

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    migrations.run_migrations(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT run_id, idea_text, mode, masky_notify_status FROM masky_runs WHERE run_id = :id"),
            {"id": "lp_preexisting"},
        ).fetchone()
    assert row == ("lp_preexisting", "a pre-existing idea from before the migration", "demo", None)


def test_migrated_database_supports_post_and_get_api_runs(tmp_path):
    """End-to-end: build the pre-commit schema, point a fresh process's app
    at it via DATABASE_URL, let it migrate on import (same as a real
    startup), then exercise POST/GET /api/runs for real.

    Runs in a subprocess rather than reloading database/models/main/api_runs
    in-process: those modules are singletons shared with every other test in
    this file's pytest session (e.g. test_api_runs.py's `from main import
    app`), and importlib.reload()'ing them here would mutate that shared
    state for every test that runs afterward -- a subprocess gets a fully
    independent Python process and module table instead."""
    import json
    import subprocess
    import sys

    db_path = tmp_path / "pre_migration.db"
    _build_pre_migration_db(db_path)

    script = """
import json
from main import app  # triggers create_all() + run_migrations() on import, same as a real startup
from fastapi.testclient import TestClient
client = TestClient(app)

r = client.post("/api/runs", json={"idea_text": "An AI assistant for international graduate students"})
assert r.status_code == 200, r.text
run_id = r.json()["run_id"]

r2 = client.get(f"/api/runs/{run_id}")
assert r2.status_code == 200, r2.text

r3 = client.get("/api/runs/lp_preexisting")
assert r3.status_code == 200, r3.text
assert r3.json()["run_id"] == "lp_preexisting"
print(json.dumps({"ok": True}))
"""
    backend_dir = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    env = {**__import__("os").environ, "DATABASE_URL": f"sqlite:///{db_path}", "PYTHONPATH": backend_dir}
    result = subprocess.run([sys.executable, "-c", script], cwd=backend_dir, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True}


# ---------- 7. fresh-database initialization still works ----------

def test_migration_is_a_no_op_on_a_freshly_created_database(tmp_path):
    """create_all() already produces every current column on a brand new
    database -- run_migrations() must find nothing to do and must not fail
    just because the columns are already there."""
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    import database as database_module
    import models  # noqa: F401 -- registers every table (incl. masky_runs) on Base.metadata
    database_module.Base.metadata.create_all(bind=engine)

    migrations.run_migrations(engine)  # must not raise
    columns = {col["name"] for col in inspect(engine).get_columns("masky_runs")}
    assert {"masky_notify_status", "masky_conversation_id"} <= columns


def test_migration_is_idempotent_when_run_twice(tmp_path):
    """Simulates two process starts in a row (or startup retried after a
    crash) -- the second run must be a no-op, not an error."""
    db_path = tmp_path / "pre_migration.db"
    _build_pre_migration_db(db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    migrations.run_migrations(engine)
    migrations.run_migrations(engine)  # must not raise "duplicate column"

    columns = {col["name"] for col in inspect(engine).get_columns("masky_runs")}
    assert {"masky_notify_status", "masky_conversation_id"} <= columns
