"""Acceptance tests: shadow experiments cannot manufacture backdated wins."""
import sqlite3

import pandas as pd

from btc_edge.config import INTERVAL_MS
from btc_edge.shadow_paper import (
    HORIZONS, STRATEGIES, install, queue_new, settle, summary, sqlite_restore,
    MIN_STOP_PCT,
)


def fixture_db():
    db = sqlite3.connect(":memory:")
    install(db)
    return db


def fixture_candles(end_bar=54):
    rows=[]
    for i in range(end_bar+1):
        rows.append({"open":50000.0, "high":50070.0, "low":49930.0,
                     "close":50000.0})
    btc=pd.DataFrame(rows,index=pd.Index(
        [i*INTERVAL_MS for i in range(end_bar+1)],name="open_time"))
    features=pd.DataFrame({"atr_at_decision":[100.0]*len(btc)},index=btc.index)
    return btc,features


def test_first_reference_queues_real_future_entries_for_four_horizons():
    db=fixture_db()
    btc,features=fixture_candles()
    observed=55*INTERVAL_MS+50_000
    q=queue_new(db,btc,features,observed_ms=observed)
    assert len(q)==4
    assert all(x["strategy_id"]=="hourly_reference" for x in q)
    assert {x["horizon_bars"] for x in q}==set(HORIZONS)
    expected=(observed//INTERVAL_MS+1)*INTERVAL_MS
    assert all(x["scheduled_entry_open_ms"]==expected for x in q)
    assert summary(db)["trials"]["queued"]==4
    assert queue_new(db,btc,features,observed_ms=observed)==[]


def test_never_settle_before_a_future_candle_is_complete():
    db=fixture_db()
    btc,features=fixture_candles()
    observed=55*INTERVAL_MS+50_000
    queue_new(db,btc,features,observed_ms=observed)
    assert settle(db,btc,settled_ms=observed)==0
    assert summary(db)["trials"]["wins"]==0
    assert summary(db)["trials"]["queued"]==4
    btc.loc[56*INTERVAL_MS]=[50000.0,51000.0,49970.0,50900.0]
    assert settle(db,btc,settled_ms=57*INTERVAL_MS)>0
    s=summary(db)["trials"]
    assert s["wins"]==4
    assert s["completed"]==4
    assert settle(db,btc,settled_ms=57*INTERVAL_MS)==0


def test_stop_first_when_both_target_and_stop_touched():
    db=fixture_db()
    btc,features=fixture_candles()
    observed=55*INTERVAL_MS+50_000
    queue_new(db,btc,features,observed_ms=observed)
    btc.loc[56*INTERVAL_MS]=[50000.0,51000.0,49000.0,50900.0]
    settle(db,btc,settled_ms=57*INTERVAL_MS)
    s=summary(db)["trials"]
    assert s["losses"]==4
    assert s["wins"]==0


def test_rejects_backfilled_signal_after_outage():
    db=fixture_db()
    btc,features=fixture_candles()
    too_late=75*INTERVAL_MS
    assert queue_new(db,btc,features,observed_ms=too_late)==[]
    assert summary(db)["trials"]["queued"]==0


def test_rebound_signal_only_after_confirming_green_candle():
    btc,features=fixture_candles(70)
    for i in range(55,70):
        btc.loc[i*INTERVAL_MS,["open","high","low","close"]]=[50000,50100,49800,49950]
    btc.loc[69*INTERVAL_MS,["open","high","low","close"]]=[49890,49950,49800,49850]
    btc.loc[70*INTERVAL_MS,["open","high","low","close"]]=[49850,50000,49840,49980]
    from btc_edge.shadow_paper import decide
    assert decide(btc,70*INTERVAL_MS,"dip_rebound")


def test_restore_is_idempotent_for_shadow_ledger():
    db=fixture_db()
    btc,features=fixture_candles()
    observed=55*INTERVAL_MS+50_000
    queue_new(db,btc,features,observed_ms=observed)
    from btc_edge.shadow_paper import COLS
    originals=db.execute("SELECT "+",".join(COLS)+" FROM shadow_trials").fetchall()
    payloads=[dict(zip(COLS,row)) for row in originals]
    assert sqlite_restore(db,payloads)==4
    assert sqlite_restore(db,payloads)==4
    assert summary(db)["trials"]["queued"]==4
