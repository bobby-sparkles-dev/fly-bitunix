"""Pre-registered, forward-only BTC paper experiments, never brokerage orders.

Three independent entry hypotheses × four holding horizons. The hourly reference
is a plumbing/baseline control, NOT a signal. All trials use Binance.US spot
OHLCV as a futures-market PROXY; prices, funding, spread and actual fees can differ.

A setup must be observed using an already-completed candle. Its simulated entry
is scheduled for the NEXT untouched full 5m candle, rather than backfilled after
seeing the outcome. No previous signal history is retroactively turned into a trade.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import INTERVAL_MS

HORIZONS = (6, 12, 24, 48)  # 30m, 1h, 2h, 4h
STRATEGIES = ("hourly_reference", "momentum_breakout", "dip_rebound")
# Same costs across all experiments; deliberately not fitted on historical outcomes.
FEE_BPS_PER_SIDE = 6.0
SLIPPAGE_BPS_PER_SIDE = 2.0
MIN_STOP_PCT = 0.006
STOP_ATR_MULTIPLE = 1.5
REWARD_TO_RISK = 1.8
MAX_DATA_AGE_MS = 15 * 60_000
# 15 minutes of buffer after each hypothetical exit for the next decision.
REST_BARS = 3
VENUE_DATA_LABEL = "Binance.US spot market data; futures-like fee assumption is only a proxy"
COLS = ("strategy_id", "horizon_bars", "signal_open_ms", "model_id",
        "observed_at", "scheduled_entry_open_ms", "entry_basis",
        "atr_at_signal", "status", "entry_fill", "stop_price",
        "target_price", "risk_price", "exit_fill", "exit_open_ms",
        "net_r", "closed_at", "close_reason")
DDL = """
CREATE TABLE IF NOT EXISTS shadow_trials (
  strategy_id TEXT NOT NULL,
  horizon_bars INTEGER NOT NULL,
  signal_open_ms INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  scheduled_entry_open_ms INTEGER NOT NULL,
  entry_basis TEXT NOT NULL,
  atr_at_signal REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('QUEUED','OPEN','WIN','LOSS','INVALID_DATA')),
  entry_fill REAL,
  stop_price REAL,
  target_price REAL,
  risk_price REAL,
  exit_fill REAL,
  exit_open_ms INTEGER,
  net_r REAL,
  closed_at TEXT,
  close_reason TEXT,
  PRIMARY KEY(strategy_id,horizon_bars,signal_open_ms)
);
CREATE INDEX IF NOT EXISTS idx_shadow_recent
  ON shadow_trials(observed_at);
"""


def utc_now(ms: int | None = None) -> str:
    if ms is None:
        ms = int(time.time() * 1000)
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def install(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    db.commit()


def decide(btc, signal_ms: int, strategy_id: str, first_reference: bool = False) -> bool:
    """Use only historical candles ending at signal_ms, never future data."""
    if strategy_id not in STRATEGIES or signal_ms not in btc.index:
        return False
    history = btc.loc[btc.index <= signal_ms]
    if len(history) < 55:
        return False
    latest = history.iloc[-1]
    before = history.iloc[-2]
    if strategy_id == "hourly_reference":
        # A single first reference observation validates queue/settlement plumbing.
        # Thereafter one observation per hour at its final closed candle.
        return first_reference or (signal_ms // INTERVAL_MS) % 12 == 11
    if strategy_id == "momentum_breakout":
        # Current bar closes above the prior hour high; no need to anticipate
        # an unconfirmed breakout or peek into the breakout's next bar.
        prior_high = float(history.iloc[-13:-1]["high"].max())
        return (float(latest["close"]) > prior_high
                and float(latest["close"]) > float(latest["open"]))
    if strategy_id == "dip_rebound":
        # Previous completed bar probed the lowest region of the last 12 bars,
        # current closed bar is green and recovered above the previous close.
        earlier = history.iloc[-13:-1]
        floor = float(earlier["low"].min())
        return (float(before["low"]) <= floor * 1.002
                and float(latest["close"]) > float(latest["open"])
                and float(latest["close"]) > float(before["close"]))
    return False


def queue_new(db: sqlite3.Connection, btc, features, score: dict | None = None,
              observed_ms: int | None = None) -> list[dict]:
    """Run each strategy without the validated-model 62% entry gate.

    One open/queued position PER experimental cohort, not portfolio-wide.
    Old or missing candles cannot generate a virtual entry.
    """
    observed_ms = int(time.time() * 1000) if observed_ms is None else int(observed_ms)
    if btc.empty or features.empty:
        return []
    signal_ms = int(btc.index.max())
    if signal_ms not in features.index:
        return []
    age = observed_ms - signal_ms - INTERVAL_MS
    if not (0 <= age <= MAX_DATA_AGE_MS):
        return []
    atr = float(features.loc[signal_ms, "atr_at_decision"])
    if not math.isfinite(atr) or atr <= 0:
        return []
    # The next *new* candle starts after observation, not the one underway.
    next_entry_ms = ((observed_ms // INTERVAL_MS) + 1) * INTERVAL_MS
    model_id = str((score or {}).get("model_id") or "untrained_reference")
    results = []
    for strategy in STRATEGIES:
        for horizon in HORIZONS:
            pending = db.execute("""
              SELECT 1 FROM shadow_trials
              WHERE strategy_id=? AND horizon_bars=? AND status IN ('QUEUED','OPEN')
              LIMIT 1
            """, (strategy,horizon)).fetchone()
            if pending:
                continue
            last = db.execute("""
              SELECT MAX(exit_open_ms) FROM shadow_trials
              WHERE strategy_id=? AND horizon_bars=?
            """, (strategy,horizon)).fetchone()[0]
            if last is not None and signal_ms <= int(last) + REST_BARS * INTERVAL_MS:
                continue
            previous = db.execute("""
              SELECT count(*) FROM shadow_trials
              WHERE strategy_id=? AND horizon_bars=?
            """, (strategy,horizon)).fetchone()[0]
            valid = decide(btc,signal_ms,strategy,first_reference=previous == 0)
            if not valid:
                continue
            basis = ("REFERENCE_ONLY" if strategy == "hourly_reference" else "PREDECLARED_PATTERN")
            result = db.execute("""
              INSERT OR IGNORE INTO shadow_trials(
                strategy_id,horizon_bars,signal_open_ms,model_id,
                observed_at,scheduled_entry_open_ms,entry_basis,
                atr_at_signal,status)
              VALUES(?,?,?,?,?,?,?,?,'QUEUED')
            """,(strategy,horizon,signal_ms,model_id,utc_now(observed_ms),
                 next_entry_ms,basis,atr))
            if result.rowcount:
                results.append({"strategy_id":strategy,"horizon_bars":horizon,
                                "signal_open_ms":signal_ms,
                                "scheduled_entry_open_ms":next_entry_ms})
    db.commit()
    return results


def settle(db: sqlite3.Connection, btc, settled_ms: int | None = None) -> int:
    """Settle only from actual completed candles, pessimistic stop-first intrabar."""
    if btc.empty:
        return 0
    last_bar = int(btc.index.max())
    slip = SLIPPAGE_BPS_PER_SIDE / 10_000
    fee = FEE_BPS_PER_SIDE / 10_000
    rows = db.execute("""
      SELECT strategy_id,horizon_bars,signal_open_ms,scheduled_entry_open_ms,
        atr_at_signal,status FROM shadow_trials
      WHERE status IN ('QUEUED','OPEN')
      ORDER BY scheduled_entry_open_ms ASC
    """).fetchall()
    changes = 0
    for strategy,horizon,signal,entry_at,atr,status in rows:
        entry_at = int(entry_at)
        if entry_at > last_bar:
            continue
        entry_bar = btc.loc[entry_at] if entry_at in btc.index else None
        key = (strategy,horizon,signal)
        if entry_bar is None:
            db.execute("""
              UPDATE shadow_trials SET status='INVALID_DATA',close_reason='ENTRY_GAP',
              closed_at=? WHERE strategy_id=? AND horizon_bars=? AND signal_open_ms=?
            """,(utc_now(settled_ms),*key))
            changes += 1
            continue
        entry = float(entry_bar["open"]) * (1+slip)
        risk = max(float(atr) * STOP_ATR_MULTIPLE,entry*MIN_STOP_PCT)
        stop = entry-risk
        target = entry+REWARD_TO_RISK*risk
        exit_raw = None
        exit_at = None
        reason = None
        upto = min(horizon,1+(last_bar-entry_at)//INTERVAL_MS)
        for offset in range(upto):
            candle_at = entry_at + offset*INTERVAL_MS
            if candle_at not in btc.index:
                reason = "HISTORY_GAP"
                break
            bar = btc.loc[candle_at]
            if float(bar["low"]) <= stop:
                exit_raw=min(stop,float(bar["open"]))
                exit_at=candle_at
                reason="STOP"
                break
            if float(bar["high"]) >= target:
                exit_raw=target
                exit_at=candle_at
                reason="TARGET"
                break
            if offset == horizon-1:
                exit_raw=float(bar["close"])
                exit_at=candle_at
                reason="TIME"
        if reason=="HISTORY_GAP":
            db.execute("""
              UPDATE shadow_trials SET status='INVALID_DATA',
                close_reason='HISTORY_GAP',closed_at=?
              WHERE strategy_id=? AND horizon_bars=? AND signal_open_ms=?
            """,(utc_now(settled_ms),*key))
            changes+=1
            continue
        if exit_raw is None:
            db.execute("""
              UPDATE shadow_trials SET status='OPEN',entry_fill=?,stop_price=?,
              target_price=?,risk_price=?
              WHERE strategy_id=? AND horizon_bars=? AND signal_open_ms=?
            """,(entry,stop,target,risk,*key))
            if status=="QUEUED":
                changes+=1
            continue
        exit_fill=float(exit_raw)*(1-slip)
        net_r=(exit_fill-entry-fee*(entry+exit_fill))/risk
        outcome="WIN" if net_r>0 else "LOSS"
        db.execute("""
          UPDATE shadow_trials SET status=?,entry_fill=?,stop_price=?,
            target_price=?,risk_price=?,exit_fill=?,exit_open_ms=?,
            net_r=?,close_reason=?,closed_at=?
          WHERE strategy_id=? AND horizon_bars=? AND signal_open_ms=?
        """,(outcome,entry,stop,target,risk,exit_fill,exit_at,net_r,
             reason,utc_now(settled_ms),*key))
        changes+=1
    db.commit()
    return changes


def summary(db: sqlite3.Connection) -> dict:
    count_rows=db.execute("""
        SELECT strategy_id,horizon_bars,status,COUNT(*)
        FROM shadow_trials GROUP BY strategy_id,horizon_bars,status
    """).fetchall()
    stats={}
    for strategy,horizon,status,count in count_rows:
        item=stats.setdefault((strategy,int(horizon)),
                              {"strategy_id":strategy,"horizon_minutes":int(horizon)*5,
                               "queued":0,"open":0,"wins":0,"losses":0,
                               "invalid":0})
        item[{"QUEUED":"queued","OPEN":"open","WIN":"wins","LOSS":"losses",
              "INVALID_DATA":"invalid"}[status]]+=int(count)
    totals={key:sum(item[key] for item in stats.values())
            for key in ("queued","open","wins","losses","invalid")}
    totals["completed"]=totals["wins"]+totals["losses"]
    totals["win_rate"]=(round(totals["wins"]/totals["completed"],4)
                        if totals["completed"] else None)
    rows=db.execute("""
        SELECT strategy_id,horizon_bars,status,observed_at,scheduled_entry_open_ms,
          entry_fill,exit_open_ms,net_r,close_reason
        FROM shadow_trials ORDER BY observed_at DESC LIMIT 12
    """).fetchall()
    by_strategy=[]
    for (strategy,horizon),item in sorted(stats.items()):
        finished=item["wins"]+item["losses"]
        item["completed"]=finished
        item["win_rate"]=round(item["wins"]/finished,4) if finished else None
        result=db.execute("""
            SELECT AVG(net_r) FROM shadow_trials WHERE strategy_id=? AND horizon_bars=?
              AND status IN ('WIN','LOSS')
        """,(strategy,horizon)).fetchone()[0]
        item["mean_net_r"]=round(float(result),4) if result is not None else None
        by_strategy.append(item)
    return {
        "status":"PAPER_RESEARCH_ONLY",
        "trials":totals,"cohorts":by_strategy,
        "strategy_count":len(STRATEGIES),
        "horizon_minutes":[h*5 for h in HORIZONS],
        "research_costs":{"fee_bps_per_side":FEE_BPS_PER_SIDE,
                          "slippage_bps_per_side":SLIPPAGE_BPS_PER_SIDE,
                          "round_trip_bps_approx":2*(FEE_BPS_PER_SIDE+SLIPPAGE_BPS_PER_SIDE),
                          "min_stop_pct":MIN_STOP_PCT,
                          "reward_to_risk":REWARD_TO_RISK},
        "recent":[{"strategy_id":x[0],"horizon_minutes":int(x[1])*5,
                   "status":x[2],"observed_at":x[3],
                   "scheduled_entry_open_ms":x[4],"entry_fill":x[5],
                   "exit_open_ms":x[6],"net_r":x[7],"close_reason":x[8]}
                  for x in rows],
        "disclaimer":(VENUE_DATA_LABEL+". Reference entries are intentionally "
                      "unfiltered controls, NOT investment recommendations. "
                      "Experiments are selected before future prices; no live orders.")
    }


PG_DDL="""CREATE TABLE IF NOT EXISTS btc_shadow_trials_v1 (
    venue TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    horizon_bars INTEGER NOT NULL,
    signal_open_ms BIGINT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (venue,strategy_id,horizon_bars,signal_open_ms)
)"""


def pg_install(cur) -> None:
    cur.execute(PG_DDL)


def pg_sync(db: sqlite3.Connection,cur) -> int:
    from psycopg.types.json import Jsonb
    rows=db.execute("SELECT "+",".join(COLS)+" FROM shadow_trials").fetchall()
    if not rows:
        return 0
    cur.executemany("""
      INSERT INTO btc_shadow_trials_v1
        (venue,strategy_id,horizon_bars,signal_open_ms,payload)
      VALUES(%s,%s,%s,%s,%s)
      ON CONFLICT(venue,strategy_id,horizon_bars,signal_open_ms)
      DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()
      WHERE btc_shadow_trials_v1.payload IS DISTINCT FROM EXCLUDED.payload
    """,[( __import__("btc_edge.market",fromlist=["PROVIDER"]).PROVIDER,
          row[0],row[1],row[2],Jsonb(dict(zip(COLS,row)))) for row in rows])
    return len(rows)


def pg_fetch(cur) -> list[dict]:
    from .market import PROVIDER
    cur.execute("""
        SELECT payload FROM btc_shadow_trials_v1
        WHERE venue=%s ORDER BY signal_open_ms
    """,(PROVIDER,))
    return [row[0] for row in cur.fetchall()]


def sqlite_restore(db: sqlite3.Connection,payloads: list[dict]) -> int:
    for payload in payloads:
        if isinstance(payload,str):
            payload=json.loads(payload)
        vals=[payload.get(name) for name in COLS]
        db.execute("""
          INSERT INTO shadow_trials ("""+",".join(COLS)+""")
          VALUES("""+",".join(["?"]*len(COLS))+""")
          ON CONFLICT(strategy_id,horizon_bars,signal_open_ms) DO UPDATE SET
          """+",".join(f"{col}=excluded.{col}" for col in COLS
                       if col not in ("strategy_id","horizon_bars","signal_open_ms")),vals)
    db.commit()
    return len(payloads)
