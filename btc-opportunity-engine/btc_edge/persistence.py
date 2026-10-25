"""Optional durable checkpoint for the existing SQLite research engine.

This is a transitional persistence adapter. PostgreSQL holds a versioned,
consistent SQLite snapshot, NOT individual relational market-data tables.
Never claim native PostgreSQL migration until the tables are ported.
"""
from __future__ import annotations
import os
import sqlite3
import tempfile
from pathlib import Path

def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))

def _connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=12)

def restore(destination: Path) -> bool:
    if not enabled():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as db:
        with db.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS research_checkpoints (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                revision BIGINT NOT NULL DEFAULT 0, data BYTEA NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("SELECT data FROM research_checkpoints WHERE singleton=TRUE")
            row = cur.fetchone()
    if not row:
        return False
    temp = destination.with_suffix(".restore.tmp")
    temp.write_bytes(bytes(row[0]))
    with sqlite3.connect(temp) as check:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            temp.unlink(missing_ok=True)
            raise RuntimeError("Postgres checkpoint failed SQLite integrity check")
    temp.replace(destination)
    return True

def checkpoint(source: Path) -> None:
    if not enabled():
        return
    with tempfile.TemporaryDirectory() as temp_dir:
        snap = Path(temp_dir) / "snapshot.sqlite"
        with sqlite3.connect(source) as live, sqlite3.connect(snap) as copy:
            live.backup(copy)
        payload = snap.read_bytes()
        with _connect() as db:
            with db.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS research_checkpoints (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    revision BIGINT NOT NULL DEFAULT 0, data BYTEA NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                cur.execute("""INSERT INTO research_checkpoints(singleton,revision,data)
                    VALUES(TRUE,1,%s)
                    ON CONFLICT(singleton) DO UPDATE SET
                    revision=research_checkpoints.revision+1,
                    data=excluded.data,updated_at=now()""",(payload,))
            db.commit()
