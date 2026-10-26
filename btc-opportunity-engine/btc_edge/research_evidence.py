"""Evidence checks for BTC forward paper experiments.

No trade recommendation is generated here. Sample dependence matters:
the same entry may contribute four horizons; each cohort contains nonoverlapping
positions but can still share the same day's market regime. We apply a
12-way multiple-comparison adjustment and resample DAY clusters rather than
falsely counting each candle as an independent observation.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist, mean

from .shadow_paper import HORIZONS, STRATEGIES

HYPOTHESES = len(HORIZONS) * len(STRATEGIES)  # explicitly preregistered 12 comparisons
ALPHA = 0.05
MIN_UNIQUE_SETUPS = 200
MIN_DISTINCT_DAYS = 20
TARGET_WIN_RATE = 0.55
MIN_BOOTSTRAP_DAYS = 8


def wilson(wins: int, n: int, simultaneous_tests: int = HYPOTHESES) -> tuple[float | None, float | None]:
    """Two-sided familywise 95% Wilson bound, descriptive not IID proof."""
    if n <= 0:
        return None,None
    z = NormalDist().inv_cdf(1-ALPHA/(2*simultaneous_tests))
    p=wins/n
    denominator=1+z*z/n
    center=(p+z*z/(2*n))/denominator
    radius=z/denominator*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return round(max(0,center-radius),4),round(min(1,center+radius),4)


def _day_key(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z","+00:00")).date().isoformat()


def daily_bootstrap_lower(results: list[tuple[str,float]],
                          iterations: int = 800, seed: int = 177) -> float | None:
    """Percentile 5th bound on mean net R by resampling whole UTC day blocks.

    An exploratory uncertainty estimate only; day blocks themselves may be
    correlated and this isn't an independently validated trading edge.
    """
    grouped=defaultdict(list)
    for at,net_r in results:
        if net_r is not None and math.isfinite(float(net_r)):
            grouped[_day_key(at)].append(float(net_r))
    days=sorted(grouped)
    if len(days)<MIN_BOOTSTRAP_DAYS:
        return None
    rng=random.Random(seed)
    values=[]
    for _ in range(iterations):
        sampled=[rng.choice(days) for _ in days]
        outcomes=[r for day in sampled for r in grouped[day]]
        if outcomes:
            values.append(mean(outcomes))
    values.sort()
    return round(values[max(0,int(.05*len(values))-1)],4) if values else None


def cohort_evidence(db) -> dict:
    from .shadow_paper import summary as paper_summary
    all_trials=db.execute("""
       SELECT strategy_id,horizon_bars,signal_open_ms,observed_at,
              status,net_r,entry_basis
       FROM shadow_trials ORDER BY observed_at,signal_open_ms
    """).fetchall()
    all_signals=set()
    all_days=set()
    cohorts=[]
    for row in all_trials:
        all_signals.add(int(row[2]))
        all_days.add(_day_key(row[3]))
    reference={}
    for strategy in STRATEGIES:
        for horizon in HORIZONS:
            records=[row for row in all_trials
                     if row[0]==strategy and int(row[1])==horizon]
            settled=[r for r in records if r[4] in ("WIN","LOSS")]
            n=len(settled)
            wins=sum(1 for r in settled if r[4]=="WIN")
            days={_day_key(r[3]) for r in settled}
            returns=[(r[3],float(r[5])) for r in settled if r[5] is not None]
            avg=round(mean(r[1] for r in returns),4) if returns else None
            lower,upper=wilson(wins,n)
            bootstrap=daily_bootstrap_lower(returns)
            if strategy=="hourly_reference":
                reference[horizon]=avg
            cohorts.append({
               "strategy_id":strategy,
               "horizon_minutes":horizon*5,
               "completed":n,
               "unique_entry_setups":len({int(r[2]) for r in settled}),
               "days_with_settled_trades":len(days),
               "wins":wins,
               "losses":n-wins,
               "win_rate":round(wins/n,4) if n else None,
               "net_r_mean":avg,
               "adjusted_wilson_lower":lower,
               "adjusted_wilson_upper":upper,
               "day_bootstrap_lower_mean_r":bootstrap,
               "meets_minimum_sample":n>=MIN_UNIQUE_SETUPS and len(days)>=MIN_DISTINCT_DAYS,
               "status":"INSUFFICIENT_INDEPENDENT_EVIDENCE"
            })
    for item in cohorts:
        ref=reference.get(item["horizon_minutes"]//5)
        item["reference_same_horizon_avg_r"]=ref
        item["difference_vs_hourly_reference_r"]=(
            round(item["net_r_mean"]-ref,4)
            if ref is not None and item["net_r_mean"] is not None else None)
        # Explicitly NO green light. We never promote after optimizing on
        # observed performance. Even statistical checks below would mean that
        # the variant earns a NEW forward holdout, not deployment.
        passes=(item["meets_minimum_sample"]
                and item["adjusted_wilson_lower"] is not None
                and item["adjusted_wilson_lower"]>TARGET_WIN_RATE
                and item["day_bootstrap_lower_mean_r"] is not None
                and item["day_bootstrap_lower_mean_r"]>0
                and item["difference_vs_hourly_reference_r"] is not None
                and item["difference_vs_hourly_reference_r"]>0)
        if passes:
            item["status"]="QUALIFIES_FOR_SEPARATE_PRECOMMITTED_FORWARD_HOLDOUT"
        elif item["meets_minimum_sample"]:
            item["status"]="NO_CONVINCING_EDGE_ESTABLISHED"
    settled_setup_ids={int(r[2]) for r in all_trials if r[4] in ("WIN","LOSS")}
    days_settled={_day_key(r[3]) for r in all_trials if r[4] in ("WIN","LOSS")}
    return {
        "status":"RESEARCH_ONLY_NOT_VALIDATED",
        "precommitted_hypotheses":HYPOTHESES,
        "all_recorded_trials":len(all_trials),
        "total_settled_trials":sum(1 for r in all_trials if r[4] in ("WIN","LOSS")),
        "distinct_signal_setups":len(all_signals),
        "distinct_settled_setups":len(settled_setup_ids),
        "distinct_days":len(all_days),
        "distinct_days_settled":len(days_settled),
        "minimum_settled_per_cohort":MIN_UNIQUE_SETUPS,
        "minimum_days_per_cohort":MIN_DISTINCT_DAYS,
        "target_win_rate":TARGET_WIN_RATE,
        "cohorts":cohorts,
        "why_not_independent":"Each underlying BTC entry can produce up to four horizon outcomes. Different strategies can enter during the same move; time-correlated trades are NOT independent observations.",
        "caveats":"No automatic model promotion. Multiple testing, changing regimes, proxy futures fees, Binance.US spot bars, slippage/funding and post-hoc tuning can create false winners. Statistical estimates are exploratory, not proof."
    }


def score_buckets(db) -> dict:
    """Does a larger pre-recorded score precede better EXECUTABLE future returns?

    Do NOT call the results probability calibration: the model predicted a
    DIFFERENT spot long bracket event, whereas these score probes evaluate
    net 30-minute directional return from a next untouched candle.
    """
    exists=db.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='btc_score_probes'
    """).fetchone()
    rows=db.execute("""
      SELECT model_id,candle_open_ms,p_long,net_return_pct,
             max_favorable_pct,max_adverse_pct
      FROM btc_score_probes
      WHERE status='COMPLETE' AND horizon_bars=6
      ORDER BY model_id,candle_open_ms
    """).fetchall() if exists else []
    by_model=defaultdict(list)
    for model,t,prob,net,mfe,mae in rows:
        if net is not None:
            by_model[model].append((int(t),float(prob),float(net),mfe,mae))
    output=[]
    bins=(0.0,.05,.10,.20,.40,.60,.80,1.000001)
    for model,observed in sorted(by_model.items()):
        last=-10**30
        selected=[]
        for item in observed:
            if item[0]-last>=30*60_000:
                selected.append(item)
                last=item[0]
        groups=[]
        for lo,hi in zip(bins,bins[1:]):
            subset=[item for item in selected if lo<=item[1]<hi]
            n=len(subset)
            groups.append({
              "range":f"{lo:.0%}–{min(hi,1):.0%}",
              "n":n,
              "predicted_avg":round(mean(x[1] for x in subset),4) if n else None,
              "observed_net_positive_rate":
                  round(mean(x[2]>0 for x in subset),4) if n else None,
              "average_net_return_pct":
                  round(mean(x[2] for x in subset),4) if n else None,
              "mean_favorable_excursion_pct":
                  round(mean(x[3] for x in subset if x[3] is not None),4)
                  if n and all(x[3] is not None for x in subset) else None,
              "mean_adverse_excursion_pct":
                  round(mean(x[4] for x in subset if x[4] is not None),4)
                  if n and all(x[4] is not None for x in subset) else None,
            })
        output.append({
          "model_id":model,
          "raw_forward_observations":len(observed),
          "spaced_observations":len(selected),
          "status":"INSUFFICIENT_FORWARD_EVIDENCE" if len(selected)<200
                    else "EXPLORATORY_PRICE_DISCRIMINATION_ONLY",
          "buckets":groups,
        })
    return {
      "status":"FORWARD_PRICE_RESPONSE_DIAGNOSTIC",
      "label":("30-minute net price-response from first untouched 5-minute candle "
               "after the forecast was actually recorded, with futures-like proxy costs"),
      "models":output,
      "warning":("NOT calibrated long-win probabilities. The model was trained on "
                 "a different 30-minute spot bracket label; these are observational "
                 "net-return bins. Overlap, regimes, multiple models and proxy "
                 "execution costs still limit confidence.")
    }


def overview(db) -> dict:
    from .research_journal import HOURS
    decisions=db.execute("SELECT COUNT(*) FROM decision_journal").fetchone()[0]
    matured=db.execute("""
        SELECT future_json FROM decision_journal WHERE future_json!='{}'
    """).fetchall()
    measured={label:0 for label in HOURS.values()}
    for (raw,) in matured:
        import json
        labels=json.loads(raw)
        for key in measured:
            if labels.get(key,{}).get("status")=="COMPLETE":
                measured[key]+=1
    return {
        "journal":{"decisions_logged":int(decisions),
                   "outcomes_logged_by_horizon":measured,
                   "definition":"Features and rejection reasons frozen at decision time; later OHLCV outcomes appended"},
        "paper":cohort_evidence(db),
        "calibration":score_buckets(db)
    }
