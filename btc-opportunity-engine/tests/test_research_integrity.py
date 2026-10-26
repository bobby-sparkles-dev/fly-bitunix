"""Safety regressions for temporal honesty and early-win evidence bias."""
import json
import sqlite3
import pandas as pd

from btc_edge.config import INTERVAL_MS
from btc_edge.score_probes import (install as install_probes,
    record as record_probe, settle as settle_probe, summary as probe_summary)
from btc_edge.research_log import (install as install_decisions,
    record as record_decision, attach_matured_outcomes)
from btc_edge.shadow_paper import install as install_shadow, COLS, sqlite_restore
from btc_edge.research_report import report as evidence_report
from btc_edge.research_evidence import cohort_evidence, score_buckets


def fixture():
    db=sqlite3.connect(":memory:")
    install_probes(db)
    install_decisions(db)
    install_shadow(db)
    bars=pd.DataFrame([{
        "open":50000., "high":50120., "low":49920., "close":50000.,
        "volume":100., "taker_buy_volume":51., "trade_count":300
    } for i in range(55)],index=pd.Index(
        [i*INTERVAL_MS for i in range(55)],name="open_time"))
    features=pd.DataFrame({
        "atr_at_decision":[110.]*len(bars),
        "btc_ret_12":[.002]*len(bars),
        "btc_eff_12":[.45]*len(bars),
        "alt_breadth_up":[.5]*len(bars)
    },index=bars.index)
    score={"timestamp_open_ms":54*INTERVAL_MS,"model_id":"m1",
           "p_tradeable":.51,"p_profitable_btc_long":.08,
           "status":"RESEARCH_ONLY","candidate":False,
           "context_complete":True,"missing_alt_features":[]}
    observed=55*INTERVAL_MS+65_000
    return db,bars,features,score,observed


def test_pre_observation_candle_is_not_used_for_forward_outcome():
    db,bars,features,score,observed=fixture()
    assert record_probe(db,score,observed_ms=observed)==4
    scheduled=((observed//INTERVAL_MS)+1)*INTERVAL_MS
    assert db.execute("SELECT MIN(scheduled_entry_open_ms) FROM btc_score_probes").fetchone()[0]==scheduled
    # A large move BEFORE observation should have no impact on the future test.
    bars.loc[54*INTERVAL_MS,["high","close"]]=[60000.,59000.]
    assert settle_probe(db,bars,now_ms=observed)==0
    assert probe_summary(db)["probes"][0]["pending"]==1
    for i in range(56,62):
        bars.loc[i*INTERVAL_MS]=[50000.,50100.,49950.,50000.]
    assert settle_probe(db,bars,now_ms=62*INTERVAL_MS)>0
    test=db.execute("""
      SELECT entry_open_ms,net_return_pct,status FROM btc_score_probes
      WHERE horizon_bars=6
    """).fetchone()
    assert test[0]==scheduled and test[2]=="COMPLETE"
    # The pre-observation 59k close was not treated as a winning entry.
    assert test[1]<0


def test_immutable_snapshot_and_later_results_never_mutate_features():
    db,bars,features,score,observed=fixture()
    assert record_decision(db,bars,features,score,[],observed)
    saved=db.execute("""
       SELECT feature_json,gate_json,observed_at FROM research_decisions_v1
    """).fetchone()
    features.loc[54*INTERVAL_MS,"btc_ret_12"]=.99
    score["p_profitable_btc_long"]=.99
    assert not record_decision(db,bars,features,score,[],observed)
    after=db.execute("""
       SELECT feature_json,gate_json,observed_at FROM research_decisions_v1
    """).fetchone()
    assert saved==after
    assert json.loads(saved[0])["btc_ret_12"]==.002
    score["p_profitable_btc_long"]=.08
    record_probe(db,score,observed_ms=observed)
    for i in range(56,62):
        bars.loc[i*INTERVAL_MS]=[50000.,50200.,49900.,50080.]
    settle_probe(db,bars,now_ms=62*INTERVAL_MS)
    assert attach_matured_outcomes(db,now_ms=62*INTERVAL_MS)==1
    future=json.loads(db.execute("SELECT future_json FROM research_decisions_v1").fetchone()[0])
    assert "30m" in future and future["30m"]["forward_only"] is True
    assert db.execute("SELECT feature_json FROM research_decisions_v1").fetchone()[0]==saved[0]


def test_evidence_report_does_not_validate_small_winning_sample():
    db,bars,features,score,observed=fixture()
    assert record_decision(db,bars,features,score,[],observed)
    from btc_edge.shadow_paper import COLS
    # One signal, three horizons: not three independent buying decisions.
    for horizon in (6,12,24):
        vals=dict.fromkeys(COLS)
        vals.update({"strategy_id":"dip_rebound","horizon_bars":horizon,
            "signal_open_ms":54*INTERVAL_MS,"model_id":"m1",
            "observed_at":"2026-10-26T10:00:00+00:00",
            "scheduled_entry_open_ms":56*INTERVAL_MS,
            "entry_basis":"PREDECLARED_PATTERN","atr_at_signal":100.,
            "status":"WIN","net_r":.75,"exit_open_ms":60*INTERVAL_MS})
        sqlite_restore(db,[vals])
    result=evidence_report(db)
    assert result["status"]=="RESEARCH_NOT_VALIDATED"
    assert result["unique_signal_timestamps"]==1
    assert result["experimental_rows_not_independent"]==3
    assert result["chance_control"]["distinct_settled_setups"]==1
    assert all(r["evidence"]=="INSUFFICIENT_INDEPENDENT_EVIDENCE"
               for r in result["cohorts"])


def test_score_ranking_uses_future_price_probes_not_legacy_lookahead():
    db,bars,features,score,observed=fixture()
    record_decision(db,bars,features,score,[],observed)
    # Former 30m label is legacy and must NOT populate forward discrimination.
    db.execute("UPDATE research_decisions_v1 SET future_json=?",
        (json.dumps({"30":{"strict_win":True,"market_tradeable":True}}),))
    db.commit()
    result=evidence_report(db)
    assert result["forecast_windows_analysed"]==0
    assert not result["score_discrimination_30m"]
    assert not score_buckets(db)["models"]
