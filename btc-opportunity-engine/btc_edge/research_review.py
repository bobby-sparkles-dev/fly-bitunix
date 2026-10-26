"""Predeclared, conservative interpretation of forward research evidence.

We NEVER silently elevate a model to "validated". All 12 shadow cohorts were
designed together and share price intervals, so nominal win rates are correlated.
The hourly reference is an uncontrolled baseline, not a matched randomised
comparator. This report keeps denominators and limitations explicit.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

from .shadow_paper import HORIZONS, STRATEGIES


def wilson(wins:int, total:int, z:float=1.96) -> dict|None:
    if total <= 0:
        return None
    p=wins/total
    z2=z*z
    denominator=1+z2/total
    midpoint=(p+z2/(2*total))/denominator
    margin=z*math.sqrt(p*(1-p)/total+z2/(4*total*total))/denominator
    return {"low":round(max(0,midpoint-margin),4),
            "high":round(min(1,midpoint+margin),4),
            "nominal_95_percent":True,
            "caution":"This assumes independent trials. Market observations cluster; interval may be optimistic."}


def clusters(timestamps:list[int], min_gap_ms:int=4*3_600_000)->int:
    """Group overlapping market episodes; don't call 12 variants 12 signals."""
    last=None
    n=0
    for t in sorted(set(int(x) for x in timestamps)):
        if last is None or t-last>=min_gap_ms:
            n+=1
            last=t
    return n


def review(db:sqlite3.Connection)->dict:
    from .score_probes import summary as probe_summary
    from .shadow_paper import summary as trial_summary
    from .research_decisions import SCHEMA_VERSION
    shadow=trial_summary(db)
    probes=probe_summary(db)
    observed=db.execute("""
       SELECT COUNT(*),MIN(logged_at),MAX(logged_at)
       FROM btc_decision_context
    """).fetchone()
    full_signals=db.execute("""
       SELECT strategy_id,signal_open_ms
       FROM shadow_trials
    """).fetchall()
    unique_times={t for _,t in full_signals}
    by_strategy={strategy:clusters([t for s,t in full_signals if s==strategy])
                 for strategy in STRATEGIES}
    day_count=db.execute("""
       SELECT COUNT(DISTINCT substr(observed_at,1,10))
       FROM shadow_trials
    """).fetchone()[0] or 0
    groups=[]
    for item in shadow["cohorts"]:
        s=item["strategy_id"]; horizon=int(item["horizon_minutes"]//5)
        settled=int(item["completed"])
        interval=wilson(int(item["wins"]),settled)
        trial_times=[t for strategy,h,t in db.execute("""
          SELECT strategy_id,horizon_bars,signal_open_ms
          FROM shadow_trials WHERE strategy_id=? AND horizon_bars=?
        """,(s,horizon))]
        independent=clusters(trial_times)
        groups.append({
            **item,
            "nominal_win_interval":interval,
            "four_hour_separated_entry_groups":independent,
            "research_confidence":"TOO_FEW_INDEPENDENT_ENTRIES"
                if independent<100 else "REQUIRES_FORWARD_OUT_OF_SAMPLE_AND_MATCHED_BASELINE",
            "claim_of_edge":False
        })
    # Score is a probability of a DIFFERENT original training label.
    # This looks only at rank ordering of a NEW time-executable price test,
    # so it is not probability calibration or a valid estimator of win rate.
    probes_30=db.execute("""
       SELECT candle_open_ms,p_long,p_market,net_return_pct,
              observed_at
       FROM btc_score_probes WHERE horizon_bars=6
         AND status='COMPLETE' ORDER BY candle_open_ms
    """).fetchall()
    thinned=[]
    last=None
    for row in probes_30:
        stamp=int(row[0])
        if last is None or stamp-last>=30*60_000:
            thinned.append(row)
            last=stamp
    bands=[]
    if thinned:
        sorted_by_score=sorted(thinned,key=lambda r:float(r[1]))
        midpoint=len(sorted_by_score)//2
        halves=(("Lower half of long scores",sorted_by_score[:midpoint]),
                ("Upper half of long scores",sorted_by_score[midpoint:]))
        for name,rows in halves:
            if rows:
                average=sum(float(x[3]) for x in rows)/len(rows)
                positive=sum(float(x[3])>0 for x in rows)
                bands.append({"label":name,"n_nonoverlapping":len(rows),
                              "mean_net_pct":round(average,4),
                              "net_positive":int(positive),
                              "positive_rate":round(positive/len(rows),4),
                              "range":(round(float(rows[0][1]),4),
                                       round(float(rows[-1][1]),4))})
    return {
        "status":"EXPLORATORY_NOT_VALIDATED",
        "decision_log":{"logged_candles":int(observed[0] or 0),
                        "first":observed[1],"last":observed[2],
                        "schema_version":SCHEMA_VERSION},
        "trading_trials":{
          "unique_signal_candles":len(unique_times),
          "four_hour_separated_market_episodes":clusters(list(unique_times)),
          "strategy_separated_episodes":by_strategy,
          "distinct_days_recorded":int(day_count),
          "across_horizons":shadow["trials"],
          "cohort_report":groups,
        },
        "score_tests":{"horizons":probes["probes"],
                       "thinned_30min_forecasts":len(thinned),
                       "ranking_comparison":bands,
                       "caution":("Score rank comparisons use later realised price movements, "
                         "not the original long-win label. They are not calibrated probabilities; "
                         "scores are not optimized against these data.")},
        "readiness":{
          "min_four_hour_separated_entries_per_strategy":100,
          "min_forward_calendar_days":30,
          "sufficient_forward_evidence":False,
          "validated_strategy_count":0,
          "thresholds_changed_automatically":False,
          "future_model_promotions_automatic":False,
          "requirements_missing":[
            "At least 100 temporally separated forward entries per strategy",
            "At least 30 different trading days across bullish, bearish and choppy regimes",
            "Predeclared candidate and matched benchmark over truly unseen forward data",
            "Positive mean net return after fees, slippage and realistic financing",
            "Uncertainty and multiple-testing adjustment across 12 experiments",
            "Out-of-sample repeatability before any parameter promotion",
          ]},
        "why_not_validated":("Early wins may be produced by one market rebound. "
              "Across 12 configurations, many results share a single entry and future path. "
              "No ranking, trading permission or change to the 62% gate is justified yet.")
    }
