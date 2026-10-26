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
