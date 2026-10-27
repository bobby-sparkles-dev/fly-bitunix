"""Public, read-only audit of actual historical BTC forecasts and candle lows.

Never recomputes forecasts with today's model; only reads forecasts that were
actually recorded. The API provides no trades, credentials or write operations.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DAY_MS = 86_400_000

def recent_forecast_audit(db_path: Path, hours: int = 24) -> dict:
    hours = max(1, min(int(hours), 24))
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - hours * 3_600_000
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        bars = [dict(row) for row in db.execute("""
            SELECT open_time,open,high,low,close FROM candles
            WHERE symbol='BTCUSDT' AND open_time>=?
            ORDER BY open_time
        """, (cutoff,))]
        forecasts = [dict(row) for row in db.execute("""
            SELECT candle_open_ms,observed_at,model_id,
                   probability_market,probability_long,
                   research_status,candidate,outcome_net_r,outcome_win,
                   evaluated_at
            FROM live_forecasts
            WHERE candle_open_ms >= ?
            ORDER BY candle_open_ms
        """, (cutoff,))]
    forecasts_by_bar = {int(f["candle_open_ms"]): f for f in forecasts}
    if not bars:
        return {"venue": "BTCUSDT", "hours": hours,
                "error": "No stored BTC candles in requested period",
                "candles": [], "forecasts": []}
    minimum = min(bars, key=lambda x: x["low"])
    low_time = int(minimum["open_time"])
    closest_before = [f for f in forecasts if int(f["candle_open_ms"]) <= low_time]
    after = [f for f in forecasts if int(f["candle_open_ms"]) > low_time]
    # 2h on either side gives enough context without huge payloads.
    neighborhood = [b for b in bars if abs(int(b["open_time"]) - low_time) <= 2 * 3_600_000]
    forecast_neighborhood = [f for f in forecasts if abs(int(f["candle_open_ms"]) - low_time) <= 2 * 3_600_000]
    def to_iso(t):
        return datetime.fromtimestamp(t/1000, tz=timezone.utc).isoformat()
    return {
        "venue": "BTCUSDT", "lookback_hours": hours,
        "server_time": to_iso(now_ms),
        "first_candle": to_iso(int(bars[0]["open_time"])),
        "last_candle": to_iso(int(bars[-1]["open_time"])),
        "candles_count": len(bars),
        "logged_forecasts_count": len(forecasts),
        "min_low": {"price": minimum["low"],
                    "candle_open_ms": low_time,
                    "candle_utc": to_iso(low_time),
                    "candle_open": minimum["open"],
                    "candle_close": minimum["close"]},
        "last_logged_forecast_at_or_before_low": closest_before[-1] if closest_before else None,
        "first_logged_forecast_after_low": after[0] if after else None,
        "candles_near_low": neighborhood,
        "forecasts_near_low": forecast_neighborhood,
        "note": "Stored contemporaneous forecasts only, not retroactively recomputed scores. Forecast candle timestamp is when the input candle opened; observed_at records when the forecast was actually logged."
    }
