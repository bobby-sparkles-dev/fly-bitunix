"""Immutable, point-in-time market context for every scored BTC candle.

Contains ONLY information known when the score was actually recorded. Never
add later prices or trade outcomes to this table. Forward outcomes belong in
the forecast/paper ledgers and are joined by candle time for analysis.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone

from .config import INTERVAL_MS
from .shadow_paper import STRATEGIES, decide

SCHEMA_VERSION = 1
RULE_VERSION = "shadow-entries-v1-2026-10-26"
COLS = ("candle_open_ms", "logged_at", "model_id", "snapshot_json")
DDL = """
CREATE TABLE IF NOT EXISTS btc_decision_context (
  candle_open_ms INTEGER PRIMARY KEY,
  logged_at TEXT NOT NULL,
  model_id TEXT NOT NULL,
  snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_logged ON btc_decision_context(logged_at);
"""
PG_DDL = """CREATE TABLE IF NOT EXISTS btc_decision_context_v1 (
  venue TEXT NOT NULL,
  candle_open_ms BIGINT NOT NULL,
  logged_at TIMESTAMPTZ NOT NULL,
  model_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (venue,candle_open_ms)
)"""
MODEL_DDL = """CREATE TABLE IF NOT EXISTS btc_model_versions_v1 (
  venue TEXT NOT NULL,
  model_id TEXT NOT NULL,
  trained_at TEXT,
  artifact JSONB NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (venue,model_id)
)"""


def install(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    db.commit()


def _num(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def record(db: sqlite3.Connection, candles, features, score: dict,
           observed_ms: int | None = None) -> bool:
    """Append once per candle; do not alter earlier observations after retraining."""
    if "BTCUSDT" not in candles or features.empty:
        return False
    btc = candles["BTCUSDT"]
    candle_ms = int(score["timestamp_open_ms"])
    if candle_ms not in btc.index or candle_ms not in features.index:
        raise ValueError("Decision candle and features are not aligned")
    observed_ms = int(time.time() * 1000) if observed_ms is None else int(observed_ms)
    observed_at = datetime.fromtimestamp(observed_ms / 1000, timezone.utc).isoformat()
    source = btc.loc[candle_ms]
    feature_row = features.loc[candle_ms]
    feature_snapshot = {str(col):_num(value) for col,value in feature_row.items()}
    bar = {name:_num(source[name]) for name in (
        "open","high","low","close","volume","taker_buy_volume","trade_count"
    ) if name in source.index}
    # These are experimental entry hypotheses, not recommendations or model
    # approvals. Even a rejected observation gets its full context preserved.
    shadow = {}
    for strategy in STRATEGIES:
        # Hourly control's first-run bootstrap exception is not a repeated rule.
        shadow[strategy] = bool(decide(btc,candle_ms,strategy,first_reference=False))
    age_ms = observed_ms - candle_ms - INTERVAL_MS
    eligibility = {
        "strict_model_candidate": bool(score.get("candidate")),
        "features_complete":bool(score.get("context_complete", False)),
        "fresh_at_observation":bool(0 <= age_ms <= 15 * 60_000),
        "shadow_conditions":shadow,
        "shadow_conditions_version":RULE_VERSION,
        "shadow_reference_explanation":"Hourly reference is a plumbing control, not a recommendation",
    }
    payload = {
        "schema_version":SCHEMA_VERSION,
        "candle_open_ms":candle_ms,
        "observed_at":observed_at,
        "venue":__import__("btc_edge.market",fromlist=["PROVIDER"]).PROVIDER,
        "model_id":str(score.get("model_id") or ""),
        "score_status":str(score.get("status") or ""),
        "market_score":_num(score.get("p_tradeable")),
        "btc_long_score":_num(score.get("p_profitable_btc_long")),
        "score_age_minutes":round(age_ms / 60_000,3),
        "missing_alt_features":list(score.get("missing_alt_features") or []),
        "candle":bar,
        "features":feature_snapshot,
        "eligibility":eligibility,
        "costs_and_exit_rules":"See immutable btc_model_versions_v1 and shadow rule version; never infer actual Bitunix fills",
        "no_future_outcomes_in_snapshot":True,
    }
    row = db.execute("""
      INSERT OR IGNORE INTO btc_decision_context
        (candle_open_ms,logged_at,model_id,snapshot_json)
      VALUES(?,?,?,?)
    """,(candle_ms,observed_at,payload["model_id"],
         json.dumps(payload,separators=(",",":"),allow_nan=False)))
    db.commit()
    return row.rowcount == 1


def pg_install(cur) -> None:
    cur.execute(PG_DDL)
    cur.execute(MODEL_DDL)


def pg_sync(db: sqlite3.Connection,cur,model_json: str | None = None) -> dict:
    from .market import PROVIDER
    from psycopg.types.json import Jsonb
    rows = db.execute("""
      SELECT candle_open_ms,logged_at,model_id,snapshot_json
      FROM btc_decision_context ORDER BY candle_open_ms DESC LIMIT 320
    """).fetchall()
    if rows:
        cur.executemany("""
          INSERT INTO btc_decision_context_v1
            (venue,candle_open_ms,logged_at,model_id,payload)
          VALUES(%s,%s,%s,%s,%s)
          ON CONFLICT(venue,candle_open_ms) DO NOTHING
        """,[(PROVIDER,int(t),obs,model,Jsonb(json.loads(snapshot)))
              for t,obs,model,snapshot in rows])
    model_count=0
    if model_json:
        artifact=json.loads(model_json)
        model_id=str(artifact["model_id"])
        trained=artifact.get("trained_at")
        cur.execute("""
          INSERT INTO btc_model_versions_v1
            (venue,model_id,trained_at,artifact)
          VALUES(%s,%s,%s,%s)
          ON CONFLICT(venue,model_id) DO NOTHING
        """,(PROVIDER,model_id,trained,Jsonb(artifact)))
        model_count=1
    return {"decisions_synced":len(rows),"model_version_checked":model_count}


def pg_recent(cur,limit: int=50) -> list[dict]:
    from .market import PROVIDER
    cur.execute("""
      SELECT candle_open_ms,logged_at,model_id,payload
      FROM btc_decision_context_v1
      WHERE venue=%s ORDER BY candle_open_ms DESC LIMIT %s
    """,(PROVIDER,max(1,min(int(limit),320))))
    return [dict(candle_open_ms=int(row[0]),logged_at=str(row[1]),
                 model_id=row[2],payload=row[3]) for row in cur.fetchall()]
