"""Forward-only, cost-aware diagnostics of *every* logged BTC model forecast.

Not a trade strategy. A test of whether higher model scores really preceded
better price changes and less adverse movement. Forecast must be recorded first;
the hypothetical entry is the NEXT untouched five-minute candle. Observed
prices are never fabricated, forward outcomes are computed only after maturity.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone

from .config import INTERVAL_MS
from .shadow_paper import FEE_BPS_PER_SIDE, SLIPPAGE_BPS_PER_SIDE

HORIZONS=(6,12,24,48)
COLS=("candle_open_ms","horizon_bars","model_id","observed_at",
      "scheduled_entry_open_ms","p_market","p_long","status",
      "entry_price","exit_price","entry_open_ms","exit_open_ms",
      "net_return_pct","gross_return_pct","max_favorable_pct",
      "max_adverse_pct","closed_at","invalid_reason")
DDL="""
CREATE TABLE IF NOT EXISTS btc_score_probes (
  candle_open_ms INTEGER NOT NULL,
  horizon_bars INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  scheduled_entry_open_ms INTEGER NOT NULL,
  p_market REAL NOT NULL,
  p_long REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','COMPLETE','INVALID_DATA')),
  entry_price REAL,
  exit_price REAL,
  entry_open_ms INTEGER,
  exit_open_ms INTEGER,
  net_return_pct REAL,
  gross_return_pct REAL,
  max_favorable_pct REAL,
  max_adverse_pct REAL,
  closed_at TEXT,
  invalid_reason TEXT,
  PRIMARY KEY(candle_open_ms,horizon_bars)
);
"""
PG_DDL="""CREATE TABLE IF NOT EXISTS btc_score_probes_v1 (
  venue TEXT NOT NULL,
  candle_open_ms BIGINT NOT NULL,
  horizon_bars INTEGER NOT NULL,
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(venue,candle_open_ms,horizon_bars)
)"""


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()


def install(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    db.commit()


def record(db: sqlite3.Connection,score:dict,observed_ms:int|None=None)->int:
    """One prospective price-response probe per scoring horizon."""
    observed_ms=int(time.time()*1000) if observed_ms is None else int(observed_ms)
    t=int(score["timestamp_open_ms"])
    age=observed_ms-t-INTERVAL_MS
    if age < 0 or age > 15*60_000 or score.get("status")=="STALE_DATA":
        return 0
    entry=((observed_ms//INTERVAL_MS)+1)*INTERVAL_MS
    count=0
    for horizon in HORIZONS:
        c=db.execute("""
          INSERT OR IGNORE INTO btc_score_probes
            (candle_open_ms,horizon_bars,model_id,observed_at,
             scheduled_entry_open_ms,p_market,p_long,status)
          VALUES(?,?,?,?,?,?,?,'PENDING')
        """,(t,horizon,str(score["model_id"]),iso(observed_ms),entry,
             float(score["p_tradeable"]),float(score["p_profitable_btc_long"])))
        count+=c.rowcount
    db.commit()
    return count


def settle(db:sqlite3.Connection,btc,now_ms:int|None=None)->int:
    """Only completed candle windows count. Never backfill missing closes."""
    if btc.empty:
        return 0
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    last_open=int(btc.index.max())
    slip=SLIPPAGE_BPS_PER_SIDE/10_000
    fee=FEE_BPS_PER_SIDE/10_000
    pending=db.execute("""
        SELECT candle_open_ms,horizon_bars,scheduled_entry_open_ms
        FROM btc_score_probes WHERE status='PENDING'
    """).fetchall()
    matured=0
    for signal,horizon,entry_at in pending:
        end_at=int(entry_at)+(int(horizon)-1)*INTERVAL_MS
        if end_at>last_open:
            continue
        window=[entry_at+n*INTERVAL_MS for n in range(horizon)]
        if not all(t in btc.index for t in window):
            db.execute("""
               UPDATE btc_score_probes SET status='INVALID_DATA',
                invalid_reason='MISSING_FUTURE_CANDLE',closed_at=?
               WHERE candle_open_ms=? AND horizon_bars=?
            """,(iso(now_ms),signal,horizon))
            matured+=1
            continue
        bars=btc.loc[window]
        entry=float(bars.iloc[0]["open"])
        exit_price=float(bars.iloc[-1]["close"])
        if entry<=0:
            continue
        gross_pct=100*(exit_price/entry-1)
        fill_entry=entry*(1+slip)
        fill_exit=exit_price*(1-slip)
        pnl=fill_exit-fill_entry-fee*(fill_exit+fill_entry)
        net_pct=100*pnl/fill_entry
        max_fav_pct=100*(float(bars["high"].max())/entry-1)
        max_adv_pct=100*(float(bars["low"].min())/entry-1)
        db.execute("""
          UPDATE btc_score_probes SET
            status='COMPLETE',entry_price=?,exit_price=?,entry_open_ms=?,
            exit_open_ms=?,net_return_pct=?,gross_return_pct=?,
            max_favorable_pct=?,max_adverse_pct=?,closed_at=?
          WHERE candle_open_ms=? AND horizon_bars=? AND status='PENDING'
        """,(entry,exit_price,entry_at,end_at,net_pct,gross_pct,
             max_fav_pct,max_adv_pct,iso(now_ms),signal,horizon))
        matured+=1
    db.commit()
    return matured


def summary(db:sqlite3.Connection)->dict:
    rows=db.execute("""
       SELECT horizon_bars,status,COUNT(*),
       AVG(net_return_pct),SUM(CASE WHEN net_return_pct>0 THEN 1 ELSE 0 END)
       FROM btc_score_probes GROUP BY horizon_bars,status
    """).fetchall()
    by_horizon=[]
    for h in HORIZONS:
        pending=sum(int(n) for horizon,status,n,*_ in rows
                    if horizon==h and status=="PENDING")
        invalid=sum(int(n) for horizon,status,n,*_ in rows
                    if horizon==h and status=="INVALID_DATA")
        good=[x for x in rows if x[0]==h and x[1]=="COMPLETE"]
        complete=int(good[0][2]) if good else 0
        avg=round(float(good[0][3]),4) if good and good[0][3] is not None else None
        wins=int(good[0][4]) if good and good[0][4] is not None else 0
        by_horizon.append({"horizon_minutes":h*5,"completed":complete,
                           "pending":pending,"invalid":invalid,
                           "net_positive":wins,"net_positive_rate":round(wins/complete,4)
                            if complete else None,"mean_net_pct":avg})
    return {"probes":by_horizon,"definition":"Executed-after-observation hypothetical price tests, not actual trades",
            "cost_bps_approx":2*(FEE_BPS_PER_SIDE+SLIPPAGE_BPS_PER_SIDE)}


def pg_install(cur):
    cur.execute(PG_DDL)


def pg_sync(db:sqlite3.Connection,cur)->int:
    from .market import PROVIDER
    from psycopg.types.json import Jsonb
    # Forward outcomes may take 4 hours: include the prior 72+ intervals.
    rows=db.execute("SELECT "+",".join(COLS)+"""
      FROM btc_score_probes ORDER BY candle_open_ms DESC,horizon_bars
      LIMIT 1440
    """).fetchall()
    if rows:
        cur.executemany("""
          INSERT INTO btc_score_probes_v1
            (venue,candle_open_ms,horizon_bars,payload)
          VALUES(%s,%s,%s,%s)
          ON CONFLICT(venue,candle_open_ms,horizon_bars) DO UPDATE
          SET payload=EXCLUDED.payload,updated_at=NOW()
          WHERE btc_score_probes_v1.payload IS DISTINCT FROM EXCLUDED.payload
        """,[(PROVIDER,int(row[0]),int(row[1]),
              Jsonb(dict(zip(COLS,row)))) for row in rows])
    return len(rows)


def pg_restore(cur)->list[dict]:
    from .market import PROVIDER
    cur.execute("""
      SELECT payload FROM btc_score_probes_v1 WHERE venue=%s
      ORDER BY candle_open_ms DESC LIMIT 2880
    """,(PROVIDER,))
    return [row[0] for row in cur.fetchall()]


def sqlite_restore(db:sqlite3.Connection,rows:list[dict])->int:
    for row in rows:
        if isinstance(row,str):
            row=json.loads(row)
        values=[row.get(key) for key in COLS]
        db.execute("INSERT INTO btc_score_probes ("+",".join(COLS)+") VALUES("+
              ",".join("?" for _ in COLS)+") ON CONFLICT(candle_open_ms,horizon_bars)"+
              " DO UPDATE SET "+
              ",".join(f"{key}=excluded.{key}" for key in COLS
                       if key not in ("candle_open_ms","horizon_bars")),values)
    db.commit()
    return len(rows)
