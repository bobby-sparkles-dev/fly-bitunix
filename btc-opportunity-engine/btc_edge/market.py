"""Download ONLY completed public Binance spot 5-minute OHLCV candles."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from collections.abc import Callable
from .config import INTERVAL_MS, SYMBOLS
from .storage import upsert_candles

BASE_URL = "https://data-api.binance.vision/api/v3/klines"

def fetch_page(symbol: str, start_ms: int, end_ms: int, retries: int = 4,
               on_retry: Callable[[dict], None] | None = None) -> list:
    query = urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "startTime": start_ms,
                                    "endTime": end_ms, "limit": 1000})
    request = urllib.request.Request(f"{BASE_URL}?{query}", headers={"User-Agent": "btc-edge-research/0.1"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            if on_retry:
                on_retry({"symbol": symbol, "kind": "http", "code": exc.code,
                          "attempt": attempt + 1, "max_attempts": retries})
            retry_after = exc.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else min(2 ** attempt, 30))
        except (TimeoutError, urllib.error.URLError):
            if attempt == retries - 1:
                raise
            if on_retry:
                on_retry({"symbol": symbol, "kind": "network",
                          "attempt": attempt + 1, "max_attempts": retries})
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("Unreachable retry condition")


def to_record(symbol: str, entry: list) -> tuple:
    return (symbol, int(entry[0]), int(entry[6]), float(entry[1]), float(entry[2]),
            float(entry[3]), float(entry[4]), float(entry[5]), float(entry[9]), int(entry[8]))


def ingest(db, days: int = 37, symbols=SYMBOLS, now_ms: int | None = None,
           on_progress: Callable[[dict], None] | None = None,
           on_retry: Callable[[dict], None] | None = None) -> dict:
    if days < 1 or days > 120:
        raise ValueError("days must be between 1 and 120")
    now_ms = now_ms or int(time.time() * 1000)
    # Close times are one millisecond before next candle open; never include forming bars.
    last_complete_end = (now_ms // INTERVAL_MS) * INTERVAL_MS - 1
    first_ms = ((now_ms - days * 86_400_000) // INTERVAL_MS) * INTERVAL_MS
    results = {}
    total_symbols = len(symbols)
    total_slots_per_symbol = max(1, (last_complete_end - first_ms) // INTERVAL_MS + 1)
    scanned_slots = 0
    ingested_rows = 0
    # Estimate actual work from checkpoints, not the full historical window.
    starts = {}
    for symbol in symbols:
        row = db.execute("SELECT MAX(open_time) FROM candles WHERE symbol=?", (symbol,)).fetchone()
        latest = int(row[0]) if row and row[0] is not None else None
        # Replay one completed candle on incremental passes to repair partial writes.
        starts[symbol] = max(first_ms, latest - INTERVAL_MS) if latest is not None else first_ms
    pending_slots = {symbol: max(0, (last_complete_end - starts[symbol]) // INTERVAL_MS + 1)
                     for symbol in symbols}
    total_slots = max(1, sum(pending_slots.values()))

    def report(symbol: str, symbol_number: int, symbol_scanned: int,
               completed_symbols: int) -> None:
        if on_progress is None:
            return
        overall_scanned = min(total_slots, scanned_slots + symbol_scanned)
        on_progress({
            "current_symbol": symbol, "symbol_number": symbol_number,
            "total_symbols": total_symbols, "completed_symbols": completed_symbols,
            "scanned_slots": overall_scanned,
            "total_slots": total_slots,
            "symbol_scanned_slots": symbol_scanned,
            "symbol_total_slots": pending_slots[symbol],
            "ingested_rows": ingested_rows,
            "percent": round(100.0 * overall_scanned / total_slots, 1),
        })

    for symbol_number, symbol in enumerate(symbols, 1):
        if not symbol.endswith("USDT"):
            raise ValueError(f"Unexpected quote asset: {symbol}")
        next_ms = starts[symbol]
        symbol_slots = pending_slots[symbol]
        saved = 0
        report(symbol, symbol_number, 0, symbol_number - 1)
        while next_ms <= last_complete_end:
            page = fetch_page(symbol, next_ms, last_complete_end, on_retry=on_retry)
            if not page:
                raise RuntimeError(f"Empty market-data page before requested end: {symbol}")
            valid = [to_record(symbol, bar) for bar in page
                     if int(bar[6]) <= last_complete_end and int(bar[0]) >= next_ms]
            count = upsert_candles(db, valid)
            saved += count
            ingested_rows += count
            advanced_ms = int(page[-1][0]) + INTERVAL_MS
            if advanced_ms <= next_ms:
                raise RuntimeError(f"Non-advancing market data response for {symbol}")
            next_ms = advanced_ms
            report(symbol, symbol_number,
                   min(symbol_slots, max(0, (next_ms - starts[symbol]) // INTERVAL_MS)),
                   symbol_number - 1)
            time.sleep(0.35)
        scanned_slots += symbol_slots
        report(symbol, symbol_number, 0, symbol_number)
        results[symbol] = saved
    return {"completed_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(), "candles": results}
