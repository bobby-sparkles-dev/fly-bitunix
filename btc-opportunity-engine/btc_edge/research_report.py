"""Conservative review of pre-registered BTC paper experiments.

No threshold optimisation, automated model promotion or live trading.
Across-horizon trials sharing one candle are correlated; confidence estimates
are computed PER cohort, with daily-block resampling for expectancy.
"""
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime,timezone
from .research_evidence import cohort_evidence, score_buckets

def wilson(wins, n, z=1.96):
    if n<1:
        return [None,None]
    p=wins/n
    denom=1+z*z/n
    centre=(p+z*z/(2*n))/denom
    spread=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/denom
    return [round(max(0,centre-spread),4),round(min(1,centre+spread),4)]

def daily_block_ci(trades, draws=500):
    """Approximate interval preserving within-day dependence."""
    days=defaultdict(list)
    for item in trades:
        key=str(item["observed_at"])[:10]
        days[key].append(float(item["net_r"]))
    keys=sorted(days)
    if len(keys)<5:
        return None
    r=random.Random(20261026)
    estimates=[]
    for _ in range(draws):
        sampled=[days[r.choice(keys)] for _ in keys]
        flat=[value for day in sampled for value in day]
        estimates.append(sum(flat)/len(flat))
    estimates.sort()
    return [round(estimates[int(0.025*(draws-1))],4),
            round(estimates[int(0.975*(draws-1))],4)]

def report(db):
    rows=db.execute("""SELECT strategy_id,horizon_bars,signal_open_ms,
         observed_at,status,net_r,exit_open_ms
         FROM shadow_trials ORDER BY signal_open_ms""").fetchall()
    groups=defaultdict(list)
    for strategy,horizon,signal,observed,status,net,exit_ms in rows:
        groups[(strategy,int(horizon))].append({
            "signal":int(signal),"observed_at":observed,
            "status":status,"net_r":net,"exit_ms":exit_ms})
    corrected = cohort_evidence(db)
    adjusted = {(item["strategy_id"],item["horizon_minutes"]):item
                for item in corrected["cohorts"]}
    result=[]
    for (strategy,horizon),trials in sorted(groups.items()):
        done=[t for t in trials if t["status"] in ("WIN","LOSS") and t["net_r"] is not None]
        wins=sum(t["status"]=="WIN" for t in done)
        n=len(done)
        expectancy=sum(float(t["net_r"]) for t in done)/n if n else None
        days=len({t["observed_at"][:10] for t in done})
        ci=wilson(wins,n)
        expected_ci=daily_block_ci(done)
        evidence = adjusted[(strategy,horizon*5)]
        # The 12 simultaneous experiments demand multiplicity correction and
        # day-block robustness. Early wins cannot pass the model gate.
        result.append({
            "strategy_id":strategy,"horizon_minutes":horizon*5,
            "entry_opportunities":len({t["signal"] for t in trials}),
            "completed":n,"wins":wins,"losses":n-wins,
            "queued":sum(t["status"]=="QUEUED" for t in trials),
            "open":sum(t["status"]=="OPEN" for t in trials),
            "win_rate":round(wins/n,4) if n else None,
            "win_rate_wilson95":ci,
            "mean_net_r":round(expectancy,4) if expectancy is not None else None,
            "mean_net_r_day_bootstrap95":expected_ci,
            "independent_days":days,
            "evidence":evidence["status"],
            "simultaneous_wilson95":[evidence["adjusted_wilson_lower"],
                                     evidence["adjusted_wilson_upper"]],
            "day_bootstrap_lower_net_r":evidence["day_bootstrap_lower_mean_r"],
            "same_horizon_reference_average_net_r":
                evidence["reference_same_horizon_avg_r"],
            "notes":"No automatic promotion. Multiple comparisons corrected across 12 variants; day-cluster bootstrap only after 8 different days. A later independent holdout is mandatory."
        })
    unique_signals={r[2] for r in rows}
    windows={int(t//(48*300000)) for t in unique_signals}
    decs=db.execute("""SELECT candle_open_ms,market_score,long_score,
        regime,gate_json,future_json
        FROM research_decisions_v1 ORDER BY candle_open_ms""").fetchall()
    reason_counts=Counter()
    regimes=Counter()
    score_groups=defaultdict(lambda: {"n":0,"winners":0,"market_n":0,"tradeable":0})
    model_pass=Counter()
    selected=set()
    for t,market,long_score,regime,gates,futures in decs:
        regimes[regime]+=1
        gate=json.loads(gates)
        for key,reason in gate.get("cohort_decisions",{}).items():
            reason_counts[key.split(":")[0]+":"+reason]+=1
        if market>=.62:model_pass["market"]+=1
        if long_score>=.62:model_pass["long"]+=1
        if market>=.62 and long_score>=.62:model_pass["both"]+=1
        future=json.loads(futures or "{}")
        # Only one timestamp in each 30-minute block for a conservative
        # descriptive calibration sample. Still not independent across days.
        block=int(t)//(6*300000)
        if block in selected or "30" not in future:
            continue
        selected.add(block)
        labels=future["30"]
        key=f"{int(min(0.999999,max(0,long_score))*10)*10}-{int(min(0.999999,max(0,long_score))*10)*10+10}%"
        item=score_groups[key]
        item["n"]+=1
        item["winners"]+=int(bool(labels.get("strict_win",False)))
        item["market_n"]+=1
        item["tradeable"]+=int(bool(labels.get("market_tradeable",False)))
    return {
        "status":"RESEARCH_NOT_VALIDATED",
        "no_live_execution":True,
        "observed_candles":len(decs),
        "forecast_windows_calibrated":len(selected),
        "strict_gate_observations":dict(model_pass),
        "market_regimes":dict(regimes),
        "entry_rejection_counts":dict(reason_counts),
        "unique_signal_timestamps":len(unique_signals),
        "separate_four_hour_blocks":len(windows),
        "experimental_rows_not_independent":len(rows),
        "cohorts":result,
        "chance_control":corrected,
        "separate_model_score_calibration":score_buckets(db),
        "score_calibration_30m":dict(sorted(score_groups.items())),
        "next_step":"Treat the first results as setup tests, not evidence of an edge. Before recommending even another research candidate, require at least 200 non-overlapping settled trades in the same cohort across 20+ UTC days, a 12-way-adjusted win-rate lower bound above 55%, positive day-bootstrap net returns, and better results than its same-horizon reference. Then run a separately committed forward holdout.",
        "important":"Winning first trades do not alter trained model scores. No hyperparameter or threshold is adjusted from these same outcomes.",
    }

def recent_decisions(db, limit=96):
    """Read-only forensic export: observed-time inputs versus later outcomes."""
    limit=max(1,min(200,int(limit)))
    rows=db.execute("""SELECT candle_open_ms,observed_at,model_id,
        market_score,long_score,score_status,regime,
        feature_json,gate_json,future_json
        FROM research_decisions_v1
        ORDER BY candle_open_ms DESC LIMIT ?""",(limit,)).fetchall()
    return {
        "mode":"FORENSIC_RESEARCH_ONLY",
        "warning":"Future outcomes are annotated AFTER the recorded decision; all model scores and features were captured beforehand. Unfilled periods were not backfilled as live predictions.",
        "count":len(rows),
        "decisions":[{
          "candle_open_ms":r[0],"observed_at":r[1],"model_id":r[2],
          "market_score":r[3],"long_score":r[4],"score_status":r[5],
          "regime":r[6],"features_at_decision":json.loads(r[7]),
          "entry_and_rejection_gates":json.loads(r[8]),
          "outcomes_observed_later":json.loads(r[9] or "{}"),
        } for r in rows],
    }
