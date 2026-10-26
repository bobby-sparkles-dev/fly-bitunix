"""Write every recorded BTC forecast and simulated trade to PostgreSQL.

The bulk OHLCV archive is checkpointed separately. This small native ledger
preserves *point-in-time* predictions across crashes between large snapshots.
Database connections remain server-side; no browser access to credentials.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from .market import PROVIDER


FORECAST_TABLE = """CREATE TABLE IF NOT EXISTS btc_forward_forecasts_v1 (
    venue TEXT NOT NULL,
    candle_open_ms BIGINT NOT NULL,
    observed_at TEXT NOT NULL,
    model_id TEXT NOT NULL,
    probability_market DOUBLE PRECISION NOT NULL,
    probability_long DOUBLE PRECISION NOT NULL,
    research_status TEXT NOT NULL,
    candidate INTEGER NOT NULL,
    outcome_net_r DOUBLE PRECISION,
    outcome_win INTEGER,
    evaluated_at TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (venue, candle_open_ms)
)"""
PAPER_TABLE = """CREATE TABLE IF NOT EXISTS btc_paper_trades_v1 (
    venue TEXT NOT NULL,
    decision_open_ms BIGINT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (venue, decision_open_ms)
)"""
FCOLS = ("candle_open_ms", "observed_at", "model_id",
         "probability_market", "probability_long", "research_status",
         "candidate", "outcome_net_r", "outcome_win", "evaluated_at")
PCOLS = ("model_id", "decision_open_ms", "observed_at", "scheduled_entry_open_ms",
         "atr_at_signal", "p_market", "p_long", "status", "entry_fill",
         "stop_price", "target_price", "risk_price", "exit_fill",
         "exit_open_ms", "net_r", "closed_at", "close_reason")


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def connect_pg():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=12)


def setup(cur) -> None:
    cur.execute(FORECAST_TABLE)
    cur.execute(PAPER_TABLE)
    from .shadow_paper import pg_install
    pg_install(cur)
    from .research_store import pg_setup
    pg_setup(cur)


def sync_from_sqlite(db: sqlite3.Connection, model_dir=None) -> dict:
    """Idempotent, immutable predictions with later outcome-only updates."""
    if not enabled():
        return {"enabled": False}
    from psycopg.types.json import Jsonb
    recent = db.execute(
        "SELECT " + ",".join(FCOLS) +
        " FROM live_forecasts ORDER BY candle_open_ms DESC LIMIT 100"
    ).fetchall()
    trades = db.execute(
        "SELECT " + ",".join(PCOLS) +
        " FROM paper_trades ORDER BY decision_open_ms DESC LIMIT 100"
    ).fetchall()
    with connect_pg() as conn:
        with conn.cursor() as cur:
            setup(cur)
            if recent:
                cur.executemany("""
                    INSERT INTO btc_forward_forecasts_v1(
                      venue,candle_open_ms,observed_at,model_id,probability_market,
                      probability_long,research_status,candidate,outcome_net_r,
                      outcome_win,evaluated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(venue,candle_open_ms) DO UPDATE SET
                      outcome_net_r=COALESCE(EXCLUDED.outcome_net_r,
                           btc_forward_forecasts_v1.outcome_net_r),
                      outcome_win=COALESCE(EXCLUDED.outcome_win,
                           btc_forward_forecasts_v1.outcome_win),
                      evaluated_at=COALESCE(EXCLUDED.evaluated_at,
                           btc_forward_forecasts_v1.evaluated_at),
                      updated_at=NOW()
                    WHERE btc_forward_forecasts_v1.outcome_win IS NULL
                      AND EXCLUDED.outcome_win IS NOT NULL
                """, [(PROVIDER, *row) for row in recent])
            if trades:
                cur.executemany("""
                    INSERT INTO btc_paper_trades_v1(
                        venue,decision_open_ms,payload)
                    VALUES(%s,%s,%s)
                    ON CONFLICT(venue,decision_open_ms) DO UPDATE SET
                      payload=EXCLUDED.payload, updated_at=NOW()
                    WHERE btc_paper_trades_v1.payload IS DISTINCT FROM EXCLUDED.payload
                """, [(PROVIDER, int(row[1]),
                       Jsonb(dict(zip(PCOLS, row)))) for row in trades])
            from .shadow_paper import pg_sync
            shadow_count=pg_sync(db,cur)
            from .research_store import pg_sync as sync_research
            research_sync = (sync_research(db,cur,model_dir) if model_dir is not None
                             else {"decision_snapshots":0,"model_saved":False})
        conn.commit()
    return {"enabled": True, "forecasts": len(recent),
            "paper_trades": len(trades),"shadow_trials":shadow_count,
            "research":research_sync}


def restore_into_sqlite(db: sqlite3.Connection) -> dict:
    """Recover records logged AFTER the last compressed database checkpoint."""
    if not enabled():
        return {"enabled": False}
    with connect_pg() as conn:
        with conn.cursor() as cur:
            setup(cur)
            cur.execute("""
                SELECT candle_open_ms,observed_at,model_id,probability_market,
                  probability_long,research_status,candidate,outcome_net_r,
                  outcome_win,evaluated_at
                FROM btc_forward_forecasts_v1 WHERE venue=%s
                ORDER BY candle_open_ms
            """, (PROVIDER,))
            predictions = cur.fetchall()
            cur.execute("""
                SELECT payload FROM btc_paper_trades_v1 WHERE venue=%s
                ORDER BY decision_open_ms
            """, (PROVIDER,))
            paper = [row[0] for row in cur.fetchall()]
            from .shadow_paper import pg_fetch
            shadow_payloads=pg_fetch(cur)
            from .research_store import restore_recent
            restored_research = restore_recent(db,cur)
    for row in predictions:
        db.execute("""
            INSERT INTO live_forecasts (
              candle_open_ms,observed_at,model_id,probability_market,
              probability_long,research_status,candidate,outcome_net_r,
              outcome_win,evaluated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(candle_open_ms) DO UPDATE SET
              outcome_net_r=COALESCE(excluded.outcome_net_r,
                live_forecasts.outcome_net_r),
              outcome_win=COALESCE(excluded.outcome_win,
                live_forecasts.outcome_win),
              evaluated_at=COALESCE(excluded.evaluated_at,
                live_forecasts.evaluated_at)
        """, row)
    for payload in paper:
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = [payload.get(col) for col in PCOLS]
        db.execute("""
            INSERT INTO paper_trades (""" + ",".join(PCOLS) + """)
            VALUES (""" + ",".join(["?"] * len(PCOLS)) + """)
            ON CONFLICT(decision_open_ms) DO UPDATE SET
            """ + ",".join(f"{col}=excluded.{col}" for col in PCOLS
                        if col != "decision_open_ms"), row)
    from .shadow_paper import install as install_shadow, sqlite_restore
    install_shadow(db)
    shadow_restored=sqlite_restore(db,shadow_payloads)
    db.commit()
    return {"enabled": True, "forecasts": len(predictions),
            "paper_trades": len(paper), "shadow_trials":shadow_restored,
            "research_decisions":restored_research}
