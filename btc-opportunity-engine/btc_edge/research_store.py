"""Native PostgreSQL persistence of immutable decision snapshots and model versions."""
import json
import sqlite3
import time
from datetime import datetime, timezone

from .market import PROVIDER

SCHEMA = """
CREATE TABLE IF NOT EXISTS btc_decision_snapshots_v1(
 venue TEXT NOT NULL,candle_open_ms BIGINT NOT NULL,
 observed_at TEXT NOT NULL, model_id TEXT NOT NULL,
 features_and_gates JSONB NOT NULL,forward_outcomes JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(venue,candle_open_ms));
CREATE TABLE IF NOT EXISTS btc_research_model_versions_v1(
 venue TEXT NOT NULL,model_id TEXT NOT NULL,trained_at TEXT NOT NULL,
 model_and_evaluation JSONB NOT NULL, archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(venue,model_id));
"""

def pg_setup(cur):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            cur.execute(statement)

def pg_sync(db,cur,model_dir):
    from psycopg.types.json import Jsonb
    # All observations are immutable. Only later independent outcomes change.
    records=db.execute("""SELECT candle_open_ms,observed_at,model_id,
           market_score,long_score,score_status,regime,feature_json,
           gate_json,future_json
           FROM research_decisions_v1 ORDER BY candle_open_ms DESC LIMIT 160""").fetchall()
    if records:
        batch=[]
        for t,observed,model,market,long_score,status,regime,features,gates,future in records:
            original={"market_score":market,"long_score":long_score,
                      "score_status":status,"regime":regime,
                      "features":json.loads(features),"gates":json.loads(gates)}
            batch.append((PROVIDER,int(t),observed,model,Jsonb(original),
                          Jsonb(json.loads(future or "{}"))))
        cur.executemany("""
          INSERT INTO btc_decision_snapshots_v1 (
            venue,candle_open_ms,observed_at,model_id,
            features_and_gates,forward_outcomes)
          VALUES(%s,%s,%s,%s,%s,%s)
          ON CONFLICT(venue,candle_open_ms) DO UPDATE SET
            forward_outcomes=btc_decision_snapshots_v1.forward_outcomes
                             || EXCLUDED.forward_outcomes,
            updated_at=NOW()
          WHERE btc_decision_snapshots_v1.forward_outcomes
              IS DISTINCT FROM
              (btc_decision_snapshots_v1.forward_outcomes
                 || EXCLUDED.forward_outcomes)
        """,batch)
    latest=model_dir/"latest.json"
    model_saved=False
    if latest.exists():
        doc=json.loads(latest.read_text())
        cur.execute("""
          INSERT INTO btc_research_model_versions_v1 (
             venue,model_id,trained_at,model_and_evaluation)
          VALUES(%s,%s,%s,%s) ON CONFLICT(venue,model_id) DO NOTHING
        """,(PROVIDER,doc["model_id"],doc["trained_at"],Jsonb(doc)))
        model_saved=True
    return {"decision_snapshots":len(records),"model_saved":model_saved}

def restore_recent(db,cur,days=14):
    from .research_log import install
    install(db)
    since=int((time.time()-days*86_400)*1000)
    cur.execute("""
       SELECT candle_open_ms,observed_at,model_id,
          features_and_gates,forward_outcomes
       FROM btc_decision_snapshots_v1
       WHERE venue=%s AND candle_open_ms>=%s
       ORDER BY candle_open_ms
    """,(PROVIDER,since))
    rows=cur.fetchall()
    for t,observed,model,raw,forward in rows:
        if isinstance(raw,str):raw=json.loads(raw)
        if isinstance(forward,str):forward=json.loads(forward)
        db.execute("""
          INSERT OR IGNORE INTO research_decisions_v1 (
            candle_open_ms,observed_at,model_id,market_score,
            long_score,score_status,regime,feature_json,gate_json,future_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)
        """,(t,observed,model,raw["market_score"],raw["long_score"],
             raw["score_status"],raw["regime"],
             json.dumps(raw["features"]),json.dumps(raw["gates"]),
             json.dumps(forward or {})))
        db.execute("""
          UPDATE research_decisions_v1 SET future_json=?
          WHERE candle_open_ms=?
        """,(json.dumps(forward or {}),t))
    db.commit()
    return len(rows)
