"""Immutable decision-time snapshots for a paper-only Bitcoin research lab."""
import json
import math
import sqlite3
from datetime import datetime, timezone
from .config import INTERVAL_MS, Settings
from .shadow_paper import HORIZONS, STRATEGIES, decide

DDL = """
CREATE TABLE IF NOT EXISTS research_decisions_v1(
 candle_open_ms INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
 model_id TEXT NOT NULL, market_score REAL NOT NULL, long_score REAL NOT NULL,
 score_status TEXT NOT NULL, regime TEXT NOT NULL,
 feature_json TEXT NOT NULL, gate_json TEXT NOT NULL,
 future_json TEXT NOT NULL DEFAULT '{}', last_evaluated_at TEXT);
"""

def install(db):
    db.executescript(DDL)
    db.commit()

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def finite(v):
    try:
        v=float(v)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):
        return None

def record(db, btc, features, score, queued, observed_ms):
    t=int(score["timestamp_open_ms"])
    if t not in features.index or t not in btc.index:
        return False
    values={str(c):finite(features.loc[t,c]) for c in features.columns}
    bar=btc.loc[t]
    for c in ("open","high","low","close","volume"):
        values["btc_"+c]=finite(bar[c])
    momentum=values.get("btc_ret_12")
    efficiency=values.get("btc_eff_12")
    regime=("UP_TREND" if momentum is not None and efficiency is not None
            and momentum>=0.002 and efficiency>=0.5 else
            "DOWN_TREND" if momentum is not None and efficiency is not None
            and momentum<=-0.002 and efficiency>=0.5 else "MIXED_OR_CHOP")
    newly={(v["strategy_id"],v["horizon_bars"]) for v in queued}
    decisions={}
    for strategy in STRATEGIES:
        for horizon in HORIZONS:
            key=strategy+":"+str(horizon*5)
            active=db.execute("""SELECT 1 FROM shadow_trials WHERE strategy_id=?
              AND horizon_bars=? AND status IN ('QUEUED','OPEN') LIMIT 1""",
              (strategy,horizon)).fetchone()
            reason=("QUEUED" if (strategy,horizon) in newly else
                    "DATA_STALE" if observed_ms-t-INTERVAL_MS>15*60000 else
                    "ALREADY_ACTIVE" if active else
                    "PATTERN_ABSENT" if not decide(btc,t,strategy) else
                    "COOLDOWN_OR_DUPLICATE")
            decisions[key]=reason
    config=Settings()
    gates={"model_candidate":bool(score.get("candidate")),
           "model_context_complete":bool(score.get("context_complete")),
           "market_threshold":config.tradeability_threshold,
           "long_threshold":config.long_threshold,
           "model_status":score["status"],
           "missing_alt_features":score.get("missing_alt_features",[]),
           "experiment_version":"shadow_v1",
           "cohort_decisions":decisions}
    cur=db.execute("""INSERT OR IGNORE INTO research_decisions_v1(
      candle_open_ms,observed_at,model_id,market_score,long_score,
      score_status,regime,feature_json,gate_json)
      VALUES(?,?,?,?,?,?,?,?,?)""",
      (t,iso(observed_ms),score["model_id"],score["p_tradeable"],
       score["p_profitable_btc_long"],score["status"],regime,
       json.dumps(values,allow_nan=False),json.dumps(gates,allow_nan=False)))
    db.commit()
    return bool(cur.rowcount)


def attach_matured_outcomes(db, now_ms=None) -> int:
    """Append later price tests only after the true forward window closes.

    The original feature_json and gate_json remain unchanged forever.
    Results explicitly state they begin at the first untouched future candle,
    not at the price known before the prediction was recorded.
    """
    import time
    if now_ms is None:
        now_ms=int(time.time()*1000)
    rows=db.execute("""
      SELECT candle_open_ms,horizon_bars,status,scheduled_entry_open_ms,
             entry_price,exit_price,net_return_pct,gross_return_pct,
             max_favorable_pct,max_adverse_pct,closed_at,invalid_reason
      FROM btc_score_probes WHERE status IN ('COMPLETE','INVALID_DATA')
      ORDER BY candle_open_ms DESC LIMIT 3000
    """).fetchall()
    per_candle={}
    for (candle,h,status,entry_ms,entry,exit_price,net,gross,favorable,
         adverse,closed,invalid) in rows:
        item=per_candle.setdefault(candle,{})
        key=str(int(h)*5)+"m"
        item[key]={
          "status":status,"simulated_entry_open_ms":entry_ms,
          "entry_price":entry,"exit_price":exit_price,
          "net_return_pct":net,"gross_return_pct":gross,
          "max_favorable_pct":favorable,"max_adverse_pct":adverse,
          "computed_at":closed,"invalid_reason":invalid,
          "forward_only":True
        }
    count=0
    for candle,future in per_candle.items():
        old=db.execute("""
          SELECT future_json FROM research_decisions_v1 WHERE candle_open_ms=?
        """,(candle,)).fetchone()
        if not old:
            continue
        old_json=json.loads(old[0] or "{}")
        changed=False
        for key,payload in future.items():
            if old_json.get(key)!=payload:
                old_json[key]=payload
                changed=True
        if changed:
            db.execute("""
              UPDATE research_decisions_v1 SET future_json=?,
                last_evaluated_at=? WHERE candle_open_ms=?
            """,(json.dumps(old_json,separators=(",",":")),iso(now_ms),candle))
            count+=1
    db.commit()
    return count
