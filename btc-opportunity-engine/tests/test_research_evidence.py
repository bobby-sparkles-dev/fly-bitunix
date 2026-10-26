"""Guard against false confidence in overlapping early BTC shadow trades."""
import sqlite3
from datetime import datetime, timezone

from btc_edge.shadow_paper import install
from btc_edge.research_evidence import (
    wilson, daily_bootstrap_lower, cohort_evidence, score_buckets,
)
from btc_edge.research_log import install as research_install
from btc_edge.score_probes import install as install_score_probes
from btc_edge.research_report import report


def setup():
    db=sqlite3.connect(":memory:")
    install(db)
    research_install(db)
    install_score_probes(db)
    db.execute("""
      CREATE TABLE live_forecasts(
        candle_open_ms INTEGER PRIMARY KEY,
        model_id TEXT,probability_long REAL,
        outcome_win INTEGER,outcome_net_r REAL
      )
    """)
    db.commit()
    return db


def test_familywise_95_percent_bound_is_more_conservative_than_one_test():
    low_adjusted,_=wilson(8,10,simultaneous_tests=12)
    low_unadjusted,_=wilson(8,10,simultaneous_tests=1)
    assert low_adjusted < low_unadjusted
    assert low_adjusted < 0.55


def test_paper_wins_from_same_entry_not_independent():
    db=setup()
    signal=100_000_000
    observed=datetime(2026,10,26,tzinfo=timezone.utc).isoformat()
    for horizon in (6,12,24,48):
        db.execute("""
           INSERT INTO shadow_trials(
             strategy_id,horizon_bars,signal_open_ms,model_id,observed_at,
             scheduled_entry_open_ms,entry_basis,atr_at_signal,status,net_r)
           VALUES(?,?,?,?,?,?,?,?,?,?)
        """,("hourly_reference",horizon,signal,"test",observed,signal+300_000,
             "REFERENCE_ONLY",60,"WIN",0.8))
    db.commit()
    result=cohort_evidence(db)
    assert result["all_recorded_trials"] == 4
    assert result["total_settled_trials"] == 4
    assert result["distinct_settled_setups"] == 1
    assert result["distinct_days_settled"] == 1
    assert all(x["status"] == "INSUFFICIENT_INDEPENDENT_EVIDENCE"
               for x in result["cohorts"])
    # Production research summary must use corrected evidence, not proclaim
    # a validated strategy just because four horizon simulations all won.
    public=report(db)
    assert public["chance_control"]["distinct_settled_setups"] == 1
    assert public["cohorts"][0]["evidence"] == "INSUFFICIENT_INDEPENDENT_EVIDENCE"


def test_no_significance_calculated_from_one_day():
    day="2026-10-26T08:00:00+00:00"
    assert daily_bootstrap_lower([(day,0.5)]*200) is None


def test_spaced_forecasts_not_equated_to_independent_candles():
    db=setup()
    for i in range(12):
        db.execute("""INSERT INTO btc_score_probes (
           candle_open_ms,horizon_bars,model_id,observed_at,
           scheduled_entry_open_ms,p_market,p_long,status,
           net_return_pct,max_favorable_pct,max_adverse_pct)
           VALUES (?,?,?,?,?,?,?,'COMPLETE',?,?,?)
        """,(i*300_000,6,"fixed","2026-10-26T08:00:00Z",
             (i+2)*300_000,.5,.1,1 if i%2 else -1,1.,-1.))
    model=score_buckets(db)["models"][0]
    assert model["raw_forward_observations"] == 12
    assert model["spaced_observations"] == 2
    assert model["status"] == "INSUFFICIENT_FORWARD_EVIDENCE"
