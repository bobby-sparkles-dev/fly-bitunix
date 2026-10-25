"""SQLite persistence for original observations, videos and independently supplied signals."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL,
  open_time INTEGER NOT NULL,
  close_time INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  taker_buy_volume REAL NOT NULL,
  trade_count INTEGER NOT NULL,
  PRIMARY KEY (symbol, open_time)
);
CREATE INDEX IF NOT EXISTS candle_time_idx ON candles(open_time);
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TEXT NOT NULL,
  url TEXT NOT NULL,
  discovered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_signals (
  signal_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL,
  source_published_at TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  source_excerpt TEXT NOT NULL,
  entry_price REAL NOT NULL,
  stop_price REAL NOT NULL,
  target_price REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
  notes TEXT NOT NULL DEFAULT ''
);
"""

def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(DDL)
    return db


def upsert_candles(db: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    db.executemany("""
      INSERT INTO candles(symbol,open_time,close_time,open,high,low,close,volume,taker_buy_volume,trade_count)
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(symbol,open_time) DO UPDATE SET
      close_time=excluded.close_time, open=excluded.open,high=excluded.high,
      low=excluded.low,close=excluded.close,volume=excluded.volume,
      taker_buy_volume=excluded.taker_buy_volume,trade_count=excluded.trade_count
    """, rows)
    db.commit()
    return len(rows)
