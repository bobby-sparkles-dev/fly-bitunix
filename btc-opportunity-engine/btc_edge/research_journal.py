"""Immutable, point-in-time BTC research journal.

Every completed, freshly observed five-minute BTC candle gets:
- all feature values known at that time, including altcoin context
- exact model identifier and predictions
- every strategy's entry/cooldown/pending reason (including rejected entries)
- later, distinct outcomes for 30m/1h/2h/4h

Future outcomes are appended ONLY once their full period has elapsed.
The immutable decision itself is never changed by later learning.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone

from .config import INTERVAL_MS, Settings
from .market import PROVIDER
from .shadow_paper import HORIZONS, STRATEGIES, REST_BARS, decide

SCHEMA_VERSION = 1
HOURS = {6: "30m", 12: "1h", 24: "2h", 48: "4h"}
DDL = """
CREATE TABLE IF NOT EXISTS decision_journal (
  decision_open_ms INTEGER PRIMARY KEY,
  observed_at TEXT NOT NULL,
  model_id TEXT NOT NULL,
  p_market REAL NOT NULL,
  p_long REAL NOT NULL,
  score_status TEXT NOT NULL,
  context_json TEXT NOT NULL,
  decisions_json TEXT NOT NULL,
  future_json TEXT NOT NULL DEFAULT '{}',
  outcomes_updated_at TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS decision_journal_observed_idx
  ON decision_journal(observed_at);
"""


def _iso(t: int | None = None) -> str:
    return datetime.fromtimestamp((t if t is not None else int(time.time()*1000))/1000,
                                  tz=timezone.utc).isoformat()


def _number(value):
    try:
        number = float(value)
        return round(number, 10) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def install(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    db.commit()


def decision_rules(db: sqlite3.Connection, btc, features,
                   score: dict, observed_ms: int) -> dict:
    """Interpret the rule that would ACTUALLY have applied at decision time."""
    signal_ms = int(score["timestamp_open_ms"])
    rows = {}
    latest = features.loc[signal_ms] if signal_ms in features.index else None
    age = observed_ms - signal_ms - INTERVAL_MS
    atr = _number(latest["atr_at_decision"]) if latest is not None else None
    current = (0 <= age <= 15*60_000)
    for strategy in STRATEGIES:
        cohorts = []
        for horizon in HORIZONS:
            already = db.execute("""
                SELECT 1 FROM shadow_trials
                WHERE strategy_id=? AND horizon_bars=?
                  AND status IN ('QUEUED','OPEN') LIMIT 1
            """,(strategy,horizon)).fetchone() is not None
            exit_ms = db.execute("""
                SELECT MAX(exit_open_ms) FROM shadow_trials
                WHERE strategy_id=? AND horizon_bars=?
            """,(strategy,horizon)).fetchone()[0]
            cooling = exit_ms is not None and signal_ms <= (
                int(exit_ms) + REST_BARS * INTERVAL_MS)
            count = db.execute("""
                SELECT COUNT(*) FROM shadow_trials
                WHERE strategy_id=? AND horizon_bars=?
            """,(strategy,horizon)).fetchone()[0]
            pattern = bool(decide(btc,signal_ms,strategy,first_reference=count == 0))
            if not current:
                result = "STALE_CANDLE"
            elif atr is None or atr <= 0:
                result = "NO_VALID_ATR"
            elif already:
                result = "ALREADY_IN_POSITION"
            elif cooling:
                result = "COOLDOWN"
            elif not pattern:
                result = "PATTERN_NOT_MET"
            else:
                result = "ELIGIBLE_FOR_QUEUE"
            cohorts.append({"minutes":horizon*5, "pattern_met":pattern,
                            "decision":result})
        rows[strategy] = cohorts
    settings = Settings()
    strict_reasons=[]
    if not current:
        strict_reasons.append("STALE_CANDLE")
    if not score.get("context_complete"):
        strict_reasons.append("INCOMPLETE_ALT_CONTEXT")
    if float(score["p_tradeable"]) < settings.tradeability_threshold:
        strict_reasons.append("MARKET_SCORE_BELOW_GATE")
    if float(score["p_profitable_btc_long"]) < settings.long_threshold:
        strict_reasons.append("LONG_SCORE_BELOW_GATE")
    return {
        "strict_model": {"eligible":not strict_reasons,
                         "market_threshold":settings.tradeability_threshold,
                         "long_threshold":settings.long_threshold,
                         "reasons":strict_reasons},
        "shadow":rows,
        "age_ms_at_decision":age,
        "entry_time_policy":"first new full 5m candle after observed_at"
    }


def record(db: sqlite3.Connection, btc, features, score: dict,
           observed_ms: int | None = None) -> bool:
    observed_ms = int(time.time()*1000) if observed_ms is None else int(observed_ms)
    if btc.empty or features.empty:
        return False
    t = int(score["timestamp_open_ms"])
    if t not in btc.index or t not in features.index:
        return False
    if not (0 <= observed_ms - t - INTERVAL_MS <= 15*60_000):
        # Do not backfill 'forecasts' that were not actually made at the time.
        return False
    row=features.loc[t]
    inputs={key:_number(value) for key,value in row.items()}
    last=btc.loc[t]
    inputs["price"]={key:_number(last[key]) for key in (
        "open","high","low","close","volume","taker_buy_volume")}
    inputs["feature_schema_version"]=SCHEMA_VERSION
    inputs["venue"]=PROVIDER
    decisions=decision_rules(db,btc,features,score,observed_ms)
    cur=db.execute("""
        INSERT OR IGNORE INTO decision_journal(
          decision_open_ms,observed_at,model_id,p_market,p_long,
          score_status,context_json,decisions_json)
        VALUES (?,?,?,?,?,?,?,?)
    """,(t,_iso(observed_ms),str(score["model_id"]),
         float(score["p_tradeable"]),float(score["p_profitable_btc_long"]),
         str(score["status"]),json.dumps(inputs,sort_keys=True,allow_nan=False),
         json.dumps(decisions,sort_keys=True,allow_nan=False)))
    db.commit()
    return cur.rowcount == 1


def mature(db: sqlite3.Connection, btc, now_ms: int | None = None,
           limit: int = 700) -> int:
    """Append only fully elapsed forward price outcomes.

    These outcome labels can be used for later analysis but NEVER rewrite
    point-in-time features or change previously recorded model predictions.
    """
    now_ms = int(time.time()*1000) if now_ms is None else int(now_ms)
    if btc.empty:
        return 0
    last_ms=int(btc.index.max())
    cutoff=last_ms - 3*86_400_000
    rows=db.execute("""
        SELECT decision_open_ms,context_json,future_json FROM decision_journal
        WHERE decision_open_ms >= ? AND decision_open_ms <= ?
          AND future_json NOT LIKE '%"4h"%'
        ORDER BY decision_open_ms ASC LIMIT ?
    """,(cutoff,last_ms,limit)).fetchall()
    changed=0
    for timestamp, raw_context, raw_future in rows:
        timestamp=int(timestamp)
        if timestamp not in btc.index:
            continue
        result=json.loads(raw_future or "{}")
        entry_bar=btc.loc[timestamp]
        reference=float(entry_bar["close"])
        for n,label in HOURS.items():
            if label in result or timestamp+n*INTERVAL_MS>last_ms:
                continue
            interval=[timestamp+i*INTERVAL_MS for i in range(1,n+1)]
            if any(t not in btc.index for t in interval):
                result[label]={"status":"DATA_GAP"}
                changed+=1
                continue
            future=btc.loc[interval]
            max_high=float(future["high"].max())
            min_low=float(future["low"].min())
            close=float(future.iloc[-1]["close"])
            result[label]={
                "status":"COMPLETE",
                "reference_close":round(reference,4),
                "end_close":round(close,4),
                "return_pct":round(100*(close/reference-1),5),
                "max_favourable_pct":round(100*(max_high/reference-1),5),
                "max_adverse_pct":round(100*(min_low/reference-1),5),
                "matured_at":_iso(now_ms),
                "definition":"Subsequent completed OHLCV versus decision close; NOT an executed trade"
            }
            changed+=1
        if result != json.loads(raw_future or "{}"):
            db.execute("""
              UPDATE decision_journal SET future_json=?,outcomes_updated_at=?
              WHERE decision_open_ms=?
            """,(json.dumps(result,sort_keys=True),_iso(now_ms),timestamp))
    db.commit()
    return changed


PG_DDL="""CREATE TABLE IF NOT EXISTS btc_decision_journal_v1 (
    venue TEXT NOT NULL,
    decision_open_ms BIGINT NOT NULL,
    observed_at TEXT NOT NULL,
    model_id TEXT NOT NULL,
    p_market DOUBLE PRECISION NOT NULL,
    p_long DOUBLE PRECISION NOT NULL,
    score_status TEXT NOT NULL,
    context JSONB NOT NULL,
    decisions JSONB NOT NULL,
    future JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(venue,decision_open_ms)
)"""


def pg_install(cur):
    cur.execute(PG_DDL)


def pg_sync(db: sqlite3.Connection,cur):
    from psycopg.types.json import Jsonb
    # Avoid shipping the whole feature history every 5 minutes. Include
    # newly observed rows plus recent ones whose future outcomes may mature.
    cur.execute("SELECT MAX(decision_open_ms) FROM btc_decision_journal_v1 WHERE venue=%s",
                (PROVIDER,))
    row=cur.fetchone()
    newest_saved=int(row[0]) if row and row[0] is not None else -1
    cutoff=int(time.time()*1000)-7*3_600_000
    rows=db.execute("""
       SELECT decision_open_ms,observed_at,model_id,p_market,p_long,
              score_status,context_json,decisions_json,future_json
       FROM decision_journal
       WHERE decision_open_ms > ? OR decision_open_ms >= ?
       ORDER BY decision_open_ms DESC LIMIT 140
    """,(newest_saved,cutoff)).fetchall()
    if not rows:
        return 0
    cur.executemany("""
      INSERT INTO btc_decision_journal_v1(
        venue,decision_open_ms,observed_at,model_id,p_market,p_long,
        score_status,context,decisions,future)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT(venue,decision_open_ms) DO UPDATE SET
        future=btc_decision_journal_v1.future || EXCLUDED.future,
        updated_at=NOW()
      WHERE btc_decision_journal_v1.future IS DISTINCT FROM
            (btc_decision_journal_v1.future || EXCLUDED.future)
    """,[(PROVIDER,r[0],r[1],r[2],r[3],r[4],r[5],
          Jsonb(json.loads(r[6])),Jsonb(json.loads(r[7])),
          Jsonb(json.loads(r[8]))) for r in rows])
    return len(rows)


def pg_restore(db: sqlite3.Connection,cur) -> int:
    cur.execute("""
        SELECT decision_open_ms,observed_at,model_id,p_market,p_long,
               score_status,context,decisions,future
        FROM btc_decision_journal_v1 WHERE venue=%s ORDER BY decision_open_ms
    """,(PROVIDER,))
    rows=cur.fetchall()
    for row in rows:
        db.execute("""
          INSERT INTO decision_journal(
            decision_open_ms,observed_at,model_id,p_market,p_long,
            score_status,context_json,decisions_json,future_json)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(decision_open_ms) DO UPDATE SET
            future_json=excluded.future_json,
            outcomes_updated_at=?
        """,(row[0],row[1],row[2],row[3],row[4],row[5],
             json.dumps(row[6]),json.dumps(row[7]),json.dumps(row[8]),_iso()))
    db.commit()
    return len(rows)
