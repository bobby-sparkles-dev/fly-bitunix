"""Decision-time / forward-only BTC research integrity tests."""
import json
import sqlite3

import pandas as pd

from btc_edge.config import INTERVAL_MS
from btc_edge.shadow_paper import install as shadow_install
from btc_edge.research_log import install as log_install, record
from btc_edge.research_outcomes import mature
from btc_edge.score_probes import install as install_probes, record as record_probe
from btc_edge.research_evidence import wilson as adjusted_wilson
from btc_edge.research_report import report, wilson, recent_decisions


def setup():
    db=sqlite3.connect(":memory:")
    shadow_install(db)
    log_install(db)
    install_probes(db)
    db.execute("""CREATE TABLE live_forecasts (
        candle_open_ms INTEGER PRIMARY KEY,
        model_id TEXT, probability_long REAL,
        outcome_win INTEGER,outcome_net_r REAL)""")
    db.commit()
    return db

def candles(end=54):
    index=[i*INTERVAL_MS for i in range(end+1)]
    values=[dict(open=50000.,high=50100.,low=49900.,
                 close=50000.,volume=10.) for _ in index]
    btc=pd.DataFrame(values,index=pd.Index(index,name="open_time"))
    features=pd.DataFrame({
        "atr_at_decision":[100.]*len(index),
        "btc_ret_12":[0.003]*len(index),
        "btc_eff_12":[0.6]*len(index),
        "alt_breadth_up":[0.5]*len(index),
        "missing_feature":[float("nan")]*len(index),
    },index=btc.index)
    return btc,features

def score(t,market=.55,long=.13):
    return dict(timestamp_open_ms=t,model_id="model-v1",
                p_tradeable=market,p_profitable_btc_long=long,
                status="RESEARCH_ONLY",candidate=False,
                context_complete=True,missing_alt_features=[])

def test_original_indicators_are_immutable():
    db=setup()
    btc,features=candles()
    t=int(btc.index.max())
    now=t+INTERVAL_MS+30000
    assert record(db,btc,features,score(t),[],now)
    assert not record(db,btc,features,score(t,.99,.99),[],now+1000)
    out=recent_decisions(db)
    assert out["count"]==1
    item=out["decisions"][0]
    assert item["market_score"]==.55
    assert item["long_score"]==.13
    assert item["features_at_decision"]["missing_feature"] is None
    assert item["regime"]=="UP_TREND"
    assert item["entry_and_rejection_gates"]["model_candidate"] is False
    assert "hourly_reference:30" in item["entry_and_rejection_gates"]["cohort_decisions"]

def test_outcomes_wait_for_all_later_completed_candles():
    db=setup()
    btc,features=candles()
    t=int(btc.index.max())
    record(db,btc,features,score(t),[],t+INTERVAL_MS+30000)
    record_probe(db,score(t),observed_ms=t+INTERVAL_MS+30000)
    assert mature(db,btc)==0
    # only 5 future bars: 30-minute label must remain missing.
    more,_=candles(60)
    assert mature(db,more)==0
    all_bars,_=candles(61)
    all_bars.loc[61*INTERVAL_MS,"close"]=50500.
    assert mature(db,all_bars)==1
    row=recent_decisions(db)["decisions"][0]
    results=row["outcomes_observed_later"]
    assert "30m" in results
    assert "1h" not in results
    assert results["30m"]["simulated_entry_open_ms"]>t
    assert results["30m"]["status"]=="COMPLETE"
    assert results["30m"]["net_return_pct"] is not None
    assert mature(db,all_bars)==0

def test_missing_future_bar_cannot_be_called_a_win():
    db=setup()
    btc,features=candles()
    t=int(btc.index.max())
    record(db,btc,features,score(t),[],t+INTERVAL_MS+20000)
    record_probe(db,score(t),observed_ms=t+INTERVAL_MS+20000)
    extended,_=candles(61)
    extended=extended.drop(58*INTERVAL_MS)
    assert mature(db,extended)==1
    outcome=recent_decisions(db)["decisions"][0]["outcomes_observed_later"]["30m"]
    assert outcome["status"]=="INVALID_DATA"
    assert outcome["net_return_pct"] is None

def test_correlated_holding_periods_are_one_unique_entry():
    db=setup()
    t=54*INTERVAL_MS
    for bars in (6,12,24,48):
        db.execute("""INSERT INTO shadow_trials(
            strategy_id,horizon_bars,signal_open_ms,model_id,
            observed_at,scheduled_entry_open_ms,entry_basis,atr_at_signal,
            status,net_r,exit_open_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("hourly_reference",bars,t,"v1",
             "2026-10-26T08:07:00+00:00",55*INTERVAL_MS,
             "REFERENCE_ONLY",100.,"WIN",0.5,(55+bars)*INTERVAL_MS))
    db.commit()
    result=report(db)
    assert result["experimental_rows_not_independent"]==4
    assert result["unique_signal_timestamps"]==1
    assert result["separate_four_hour_blocks"]==1
    assert all(x["evidence"]=="INSUFFICIENT_INDEPENDENT_EVIDENCE" for x in result["cohorts"])
    assert all(x["win_rate_wilson95"][0] < .55 for x in result["cohorts"])

def test_zero_forecasts_never_validate_random_wins():
    db=setup()
    result=report(db)
    assert result["status"]=="RESEARCH_NOT_VALIDATED"
    assert result["observed_candles"]==0
    assert result["no_live_execution"] is True
    assert adjusted_wilson(10,10)[0]<.55
