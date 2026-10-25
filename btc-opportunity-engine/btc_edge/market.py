"""Download ONLY completed public Binance spot 5-minute OHLCV candles."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from .config import INTERVAL_MS, SYMBOLS
from .storage import upsert_candles

BASE_URL = "https://data-api.binance.vision/api/v3/klines"

def fetch_page(symbol: str, start_ms: int, end_ms: int, retries: int = 4) -> list:
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
            retry_after = exc.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else min(2 ** attempt, 30))
        except (TimeoutError, urllib.error.URLError):
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("Unreachable retry condition")


def to_record(symbol: str, entry: list) -> tuple:
    return (symbol, int(entry[0]), int(entry[6]), float(entry[1]), float(entry[2]),
            float(entry[3]), float(entry[4]), float(entry[5]), float(entry[9]), int(entry[8]))


def ingest(db, days: int = 37, symbols=SYMBOLS, now_ms: int | None = None) -> dict:
    if days < 1 or days > 120:
        raise ValueError("days must be between 1 and 120")
    now_ms = now_ms or int(time.time() * 1000)
    # Close times are one millisecond before next candle open; never include forming bars.
    last_complete_end = (now_ms // INTERVAL_MS) * INTERVAL_MS - 1
    first_ms = ((now_ms - days * 86_400_000) // INTERVAL_MS) * INTERVAL_MS
    results = {}
    for symbol in symbols:
        if not symbol.endswith("USDT"):
            raise ValueError(f"Unexpected quote asset: {symbol}")
        next_ms = first_ms
        saved = 0
        while next_ms <= last_complete_end:
            page = fetch_page(symbol, next_ms, last_complete_end)
            if not page:
                break
            valid = [to_record(symbol, bar) for bar in page
                     if int(bar[6]) <= last_complete_end and int(bar[0]) >= next_ms]
            saved += upsert_candles(db, valid)
            advanced_ms = int(page[-1][0]) + INTERVAL_MS
            if advanced_ms <= next_ms:
                raise RuntimeError(f"Non-advancing market data response for {symbol}")
            next_ms = advanced_ms
            time.sleep(0.07)
        results[symbol] = saved
    return {"completed_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(), "candles": results}
