"""Read-only, same-origin chart data from the *same candles* used by the research worker.

No exchange requests from visitors, no fabricated prices and no credentials.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from .config import INTERVAL_MS, SYMBOLS
from .market import PROVIDER


def market_snapshot(database: Path, now_ms: int | None = None) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not Path(database).is_file():
        raise FileNotFoundError("Candle database not yet restored or initialised")
    data = {}
    with sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro",
                         uri=True, timeout=3.0) as db:
        for symbol in SYMBOLS:
            limit = 140 if symbol == "BTCUSDT" else 20
            rows = db.execute("""
                SELECT open_time, open, high, low, close, volume
                FROM candles WHERE symbol = ? AND close_time < ?
                ORDER BY open_time DESC LIMIT ?
            """, (symbol, now_ms, limit)).fetchall()
            data[symbol] = [list(row) for row in reversed(rows)]
    btc = data["BTCUSDT"]
    if len(btc) < 30:
        raise ValueError("BTC candle history is not ready")
    last_open = int(btc[-1][0])
    age_minutes = max(0, (now_ms - last_open - INTERVAL_MS) / 60_000)
    return {
        "source": "backend_saved_completed_candles",
        "venue": PROVIDER,
        "server_time_ms": now_ms,
        "last_btc_open_ms": last_open,
        "age_minutes": round(age_minutes, 1),
        "stale": age_minutes > 15,
        "symbols": data,
        "expected_symbols": len(SYMBOLS),
    }
