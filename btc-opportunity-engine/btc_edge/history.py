"""Paced, incremental backfill of historical completed bars.

Only appends bars older than the currently oldest bar for each symbol.
The recent candle updater remains independent and higher priority.
No exchange venue switching or synthetic data.
"""
from __future__ import annotations
import time
from collections.abc import Callable
from .config import INTERVAL_MS, SYMBOLS
from .market import fetch_page, to_record
from .storage import upsert_candles

DAY_MS = 86_400_000
ARCHIVE_DAYS = 180
# Limit to five API pages per research cycle to protect the shared IP quota.
PAGES_PER_CYCLE = 5


def coverage(db, symbols=SYMBOLS, now_ms=None) -> dict:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    result = {}
    for symbol in symbols:
        first, last, count = db.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM candles WHERE symbol=?",
            (symbol,)
        ).fetchone()
        oldest_days = max(0.0, (now_ms - int(first)) / DAY_MS) if first is not None else 0.0
        latest_age_minutes = ((now_ms - int(last) - INTERVAL_MS) / 60_000
                              if last is not None else None)
        result[symbol] = {
            "oldest_open_ms": int(first) if first is not None else None,
            "latest_open_ms": int(last) if last is not None else None,
            "rows": int(count),
            "oldest_days_ago": round(oldest_days, 2),
            "last_candle_age_minutes": round(latest_age_minutes, 2)
                if latest_age_minutes is not None else None
        }
    minimum = min((row["oldest_days_ago"] for row in result.values()), default=0)
    return {
        "target_days": ARCHIVE_DAYS,
        "minimum_days_across_symbols": round(minimum, 2),
        "percent": round(100 * min(minimum, ARCHIVE_DAYS) / ARCHIVE_DAYS, 1),
        "symbols": result,
        "all_symbols_ready_for_74_day_window": minimum >= 75,
        "archive_complete": minimum >= ARCHIVE_DAYS - 0.02
    }


def backfill(db, symbols=SYMBOLS, now_ms=None, pages=PAGES_PER_CYCLE,
             on_progress: Callable[[dict], None] | None = None,
             on_retry: Callable[[dict], None] | None = None) -> dict:
    """Grow shallowest archives first; no querying candles already in storage.

    A missing or truncated page is a visible error, not artificial progress.
    The resulting coverage is based on actual stored timestamps.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if not 0 <= pages <= 50:
        raise ValueError("pages must be between 0 and 50")
    cutoff = ((now_ms - ARCHIVE_DAYS * DAY_MS) // INTERVAL_MS) * INTERVAL_MS
    first_write = 0
    for _ in range(pages):
        state = coverage(db, symbols, now_ms)
        candidates = [(name, data) for name, data in state["symbols"].items()
                      if data["oldest_open_ms"] is not None
                      and data["oldest_open_ms"] > cutoff]
        if not candidates:
            break
        # Select least historical coverage to ensure every market reaches 75d.
        symbol, info = min(candidates, key=lambda row: (row[1]["oldest_days_ago"], row[0]))
        oldest = info["oldest_open_ms"]
        start = max(cutoff, oldest - 1000 * INTERVAL_MS)
        last_prior_open = oldest - INTERVAL_MS
        page = fetch_page(symbol, start, last_prior_open, on_retry=on_retry)
        if not page:
            raise RuntimeError(f"{symbol} has no earlier archive candles at {start}; cannot claim 180d coverage")
        candles = [to_record(symbol, row) for row in page
                   if start <= int(row[0]) <= last_prior_open and int(row[6]) < oldest]
        if not candles:
            raise RuntimeError(f"{symbol} archive response contained no earlier completed bars")
        # A successful request may contain fewer than the requested bars due to venue history.
        db_count = upsert_candles(db, candles)
        first_write += db_count
        if on_progress:
            on_progress({"symbol": symbol, "rows_added": db_count,
                         "total_added_this_cycle": first_write,
                         "coverage": coverage(db, symbols, now_ms)})
        time.sleep(0.6)
    final = coverage(db, symbols, now_ms)
    final["rows_added_this_cycle"] = first_write
    final["pages_requested"] = pages
    return final
