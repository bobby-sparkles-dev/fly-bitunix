"""Forward-only, single-position simulated BTC spot longs.

Records are created AFTER a qualifying forecast is observed. The entry is the
first full 5m candle opening strictly after observation, never the historical
"next candle" that could already be underway. No orders or broker APIs.
"""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime, timezone

from .config import INTERVAL_MS, Settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def install(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_id TEXT NOT NULL,
        decision_open_ms INTEGER NOT NULL UNIQUE,
        observed_at TEXT NOT NULL,
        scheduled_entry_open_ms INTEGER NOT NULL,
        atr_at_signal REAL NOT NULL,
        p_market REAL NOT NULL,
        p_long REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        entry_fill REAL,
        stop_price REAL,
        target_price REAL,
        risk_price REAL,
        exit_fill REAL,
        exit_open_ms INTEGER,
        net_r REAL,
        closed_at TEXT,
        close_reason TEXT
    )
    """)
    db.commit()


def queue_signal(db: sqlite3.Connection, score: dict, atr: float,
                 observed_ms: int | None = None, settings: Settings | None = None) -> bool:
    """Queue only one *new* forecast at a time; never backdate the entry."""
    settings = settings or Settings()
    observed_ms = int(time.time() * 1000) if observed_ms is None else int(observed_ms)
    if not score.get("candidate") or score.get("status") == "STALE_DATA":
        return False
    if not math.isfinite(float(atr)) or atr <= 0:
        return False
    if db.execute("SELECT 1 FROM paper_trades WHERE status IN ('QUEUED','OPEN') LIMIT 1").fetchone():
        return False
    signal_bar = int(score["timestamp_open_ms"])
    # Require current score, not a historical guess served after an outage.
    if observed_ms < signal_bar + INTERVAL_MS:
        return False
    if observed_ms - (signal_bar + INTERVAL_MS) > settings.max_data_staleness_minutes * 60_000:
        return False
    entry_at = ((observed_ms // INTERVAL_MS) + 1) * INTERVAL_MS
    cursor = db.execute("""
        INSERT OR IGNORE INTO paper_trades
        (model_id,decision_open_ms,observed_at,scheduled_entry_open_ms,
         atr_at_signal,p_market,p_long,status)
        VALUES (?,?,?,?,?,?,?,'QUEUED')
    """, (score["model_id"], signal_bar,
          datetime.fromtimestamp(observed_ms / 1000, timezone.utc).isoformat(),
          entry_at, float(atr), float(score["p_tradeable"]),
          float(score["p_profitable_btc_long"])))
    db.commit()
    return cursor.rowcount == 1


def settle(db: sqlite3.Connection, btc, settings: Settings | None = None) -> None:
    """Advance queued/open paper positions using COMPLETED candles only.

    The earliest possible recorded entry is strictly AFTER the real forecast
    timestamp. Stops are prioritised when the same candle touches both levels.
    Missing candle history cannot silently produce a win or loss.
    """
    settings = settings or Settings()
    if btc.empty:
        return
    rows = db.execute("""
        SELECT id,scheduled_entry_open_ms,atr_at_signal,status
        FROM paper_trades WHERE status IN ('QUEUED','OPEN') ORDER BY id
    """).fetchall()
    if not rows:
        return
    last_open = int(btc.index.max())
    slip = settings.slippage_bps_per_side / 10_000
    fee = settings.fee_bps_per_side / 10_000
    for trade_id, entry_at, atr, status in rows:
        entry_at = int(entry_at)
        if entry_at > last_open:
            continue
        bar0 = btc.loc[entry_at] if entry_at in btc.index else None
        if bar0 is None:
            # Data hole: no invented entry or exit.
            db.execute("""UPDATE paper_trades SET status='INVALID_DATA',
                          close_reason='ENTRY_CANDLE_MISSING',closed_at=?
                          WHERE id=?""", (now_iso(), trade_id))
            continue
        entry = float(bar0["open"]) * (1 + slip)
        risk = max(settings.stop_atr_multiple * atr, entry * settings.min_stop_pct)
        stop, target = entry - risk, entry + settings.reward_to_risk * risk
        db.execute("""UPDATE paper_trades SET status='OPEN',entry_fill=?,
            risk_price=?, stop_price=?,target_price=? WHERE id=?""",
                   (entry, risk, stop, target, trade_id))
        max_seen = min(settings.horizon_bars, 1 + (last_open - entry_at) // INTERVAL_MS)
        exit_raw = None
        exit_open = None
        reason = None
        for n in range(max_seen):
            open_at = entry_at + n * INTERVAL_MS
            if open_at not in btc.index:
                reason, exit_open = "DATA_GAP", open_at
                break
            bar = btc.loc[open_at]
            low, high = float(bar["low"]), float(bar["high"])
            if low <= stop:
                exit_raw, reason, exit_open = min(stop, float(bar["open"])), "STOP", open_at
                break
            if high >= target:
                exit_raw, reason, exit_open = target, "TARGET", open_at
                break
            if n == settings.horizon_bars - 1:
                exit_raw, reason, exit_open = float(bar["close"]), "TIME", open_at
        if reason == "DATA_GAP":
            db.execute("""UPDATE paper_trades SET status='INVALID_DATA',
                      close_reason=?,closed_at=? WHERE id=?""",
                       (reason, now_iso(), trade_id))
        elif exit_raw is not None:
            exit_fill = exit_raw * (1 - slip)
            net_r = (exit_fill - entry - fee * (entry + exit_fill)) / risk
            outcome = "WIN" if net_r > 0 else "LOSS"
            db.execute("""UPDATE paper_trades SET status=?,exit_fill=?,
                exit_open_ms=?, net_r=?,close_reason=?,closed_at=? WHERE id=?""",
                       (outcome, exit_fill, exit_open, net_r, reason, now_iso(), trade_id))
    db.commit()


def summary(db: sqlite3.Connection, settings: Settings | None = None) -> dict:
    """Keep historical holdout statistics separate from live-forward trials."""
    settings = settings or Settings()
    counts = {status: count for status, count in db.execute(
        "SELECT status,COUNT(*) FROM paper_trades GROUP BY status")}
    wins, losses = counts.get("WIN", 0), counts.get("LOSS", 0)
    closed = wins + losses
    net_sum = db.execute(
        "SELECT COALESCE(SUM(net_r),0) FROM paper_trades WHERE status IN ('WIN','LOSS')"
    ).fetchone()[0]
    active = db.execute("""
        SELECT id,status,observed_at,scheduled_entry_open_ms,entry_fill,stop_price,
               target_price,p_market,p_long FROM paper_trades
        WHERE status IN ('QUEUED','OPEN') ORDER BY id DESC LIMIT 1
    """).fetchone()
    recent = db.execute("""
        SELECT status,observed_at,entry_fill,net_r,close_reason FROM paper_trades
        ORDER BY id DESC LIMIT 8
    """).fetchall()
    pending = None
    if active:
        pending = dict(zip(("id","status","observed_at","entry_open_ms","entry_fill",
                            "stop","target","p_market","p_long"), active))
    return {
        "queued": counts.get("QUEUED", 0),
        "open": counts.get("OPEN", 0),
        "completed": closed,
        "wins": wins, "losses": losses,
        "invalid": counts.get("INVALID_DATA", 0),
        "win_rate": round(wins / closed, 4) if closed else None,
        "total_net_r": round(net_sum, 3),
        "mean_net_r": round(net_sum / closed, 4) if closed else None,
        "research_target_win_rate": 0.55,
        "sample_target": 100,
        "enough_for_initial_review": closed >= 100,
        "active": pending,
        "recent": [dict(zip(("status","observed_at","entry_fill","net_r","reason"), row))
                   for row in recent],
        "explanation": ("One simulated BTC spot long at a time. New entries are scheduled "
                        "strictly after the forecast is recorded; wins and losses include "
                        "fee/slippage assumptions and are not proof of future performance.")
    }
