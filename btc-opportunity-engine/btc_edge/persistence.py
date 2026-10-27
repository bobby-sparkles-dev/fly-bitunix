"""Durable, venue-isolated SQLite/model snapshot bridge to PostgreSQL.

This is intentionally an interim persistence layer, not native PG tables.
Checkpoint and model are written in one transaction to a versioned row.
Existing legacy global-only snapshots can be restored non-destructively.
"""
from __future__ import annotations
import json
import os
import sqlite3
import zlib
import tempfile
from pathlib import Path

def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))

def namespace() -> str:
    from .market import PROVIDER
    return PROVIDER

def _connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=12)

DDL = """CREATE TABLE IF NOT EXISTS research_snapshots_v2 (
    venue TEXT PRIMARY KEY,
    revision BIGINT NOT NULL,
    data BYTEA NOT NULL,
    model_json TEXT,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

def restore(destination: Path, model_dir: Path) -> bool:
    if not enabled():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT data, model_json FROM research_snapshots_v2 WHERE venue=%s", (namespace(),))
            record = cur.fetchone()
            if record is None and namespace() == "binance_global":
                # Preserve snapshots produced by the previous implementation.
                cur.execute("SELECT to_regclass('public.research_checkpoints')")
                if cur.fetchone()[0] is not None:
                    cur.execute("SELECT data FROM research_checkpoints WHERE singleton=TRUE")
                    legacy = cur.fetchone()
                    if legacy:
                        record = (legacy[0], None)
        conn.commit()
    if record is None:
        return False
    data, model_json = record
    temp = destination.with_suffix(".restore.tmp")
    raw = bytes(data)
    if raw.startswith(b"ZLIB1"):
        raw = zlib.decompress(raw[5:])
        if len(raw) > 250_000_000:
            raise RuntimeError("Unreasonably large restored SQLite snapshot")
    temp.write_bytes(raw)
    try:
        with sqlite3.connect(temp) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Postgres SQLite snapshot integrity check failed")
        # Validate model before replacing the database; never execute serialized model code.
        if model_json is not None:
            parsed = json.loads(model_json)
            if not isinstance(parsed, dict) or "models" not in parsed:
                raise RuntimeError("Invalid saved model JSON")
        temp.replace(destination)
        if model_json is not None:
            temp_model = model_dir / "latest.tmp"
            temp_model.write_text(model_json)
            temp_model.replace(model_dir / "latest.json")
    finally:
        temp.unlink(missing_ok=True)
    return True

def checkpoint(source: Path, model_dir: Path) -> None:
    if not enabled():
        return
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        with sqlite3.connect(source) as live, sqlite3.connect(snapshot) as copy:
            live.backup(copy)
        payload = b"ZLIB1" + zlib.compress(snapshot.read_bytes(), level=6)
        file = model_dir / "latest.json"
        models = file.read_text() if file.is_file() else None
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("""INSERT INTO research_snapshots_v2
                (venue,revision,data,model_json,saved_at)
                VALUES(%s,1,%s,%s,now())
                ON CONFLICT(venue) DO UPDATE SET
                revision=research_snapshots_v2.revision+1,
                data=excluded.data,model_json=excluded.model_json,
                saved_at=now()""", (namespace(), payload, models))
            conn.commit()
