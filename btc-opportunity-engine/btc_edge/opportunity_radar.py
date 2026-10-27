"""Human-in-the-loop market opportunity radar, NOT a trading system.

Predeclared v1 heuristic based only on completed decision-time market features.
Its labels measure future BTC/altcoin opportunity windows, not filled trades.
Original observations are immutable; future outcomes are added separately.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from datetime import datetime, timezone

from .config import INTERVAL_MS
from .market import PROVIDER

VERSION = "long-window-radar-v1-2026-10-27"
HORIZONS = (6, 12, 24, 48)  # 30m, 60m, 120m, 240m
MIN_ALTS = 7
MAX_DATA_AGE_MS = 15 * 60_000

OBS_DDL = """CREATE TABLE IF NOT EXISTS opportunity_radar_observations (
  candle_open_ms INTEGER PRIMARY KEY,
  observed_at TEXT NOT NULL,
  scheduled_start_ms INTEGER NOT NULL,
  ruleset_id TEXT NOT NULL,
  state TEXT NOT NULL,
  score INTEGER NOT NULL,
  context_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL
)"""
OUT_DDL = """CREATE TABLE IF NOT EXISTS opportunity_radar_outcomes (
  candle_open_ms INTEGER NOT NULL,
  horizon_bars INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','COMPLETE','INVALID_DATA')),
  result_json TEXT,
  evaluated_at TEXT,
  PRIMARY KEY(candle_open_ms,horizon_bars)
)"""
PG_OBS_DDL = """CREATE TABLE IF NOT EXISTS btc_opportunity_radar_v1 (
 venue TEXT NOT NULL,
 candle_open_ms BIGINT NOT NULL,
 payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(venue,candle_open_ms)
)"""
PG_OUT_DDL = """CREATE TABLE IF NOT EXISTS btc_opportunity_outcomes_v1 (
 venue TEXT NOT NULL,
 candle_open_ms BIGINT NOT NULL,
 horizon_bars INTEGER NOT NULL,
 status TEXT NOT NULL,
 payload JSONB,
 evaluated_at TEXT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(venue,candle_open_ms,horizon_bars)
)"""


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def install(db: sqlite3.Connection) -> None:
    db.execute(OBS_DDL)
    db.execute(OUT_DDL)
    db.commit()


def _number(row, key):
    try:
        n = float(row[key])
        return n if math.isfinite(n) else None
    except (KeyError, ValueError, TypeError):
        return None


def reading(features, score: dict, observed_ms: int | None = None) -> dict:
    """Versioned exploratory radar, independent of BTC bracket-win probabilities."""
    observed_ms = int(time.time() * 1000) if observed_ms is None else int(observed_ms)
    candle_ms = int(score["timestamp_open_ms"])
    age = observed_ms - candle_ms - INTERVAL_MS
    row = features.loc[candle_ms] if candle_ms in features.index else {}
    values = {key: _number(row, key) for key in (
        "btc_ret_6", "btc_ret_12", "btc_eff_6", "btc_ema12_distance",
        "btc_ema48_distance", "btc_volume_z", "btc_buy_ratio",
        "alt_breadth_up", "alt_mean_return_6", "btc_atr_pct")}
    alt_columns = [c for c in getattr(features, "columns", [])
                   if c.endswith("_ret_6") and c != "btc_ret_6"]
    covered = sum(_number(row, c) is not None for c in alt_columns)
    valid = (0 <= age <= MAX_DATA_AGE_MS
             and score.get("status") != "STALE_DATA"
             and covered >= MIN_ALTS
             and all(v is not None for v in values.values()))
    reasons = []
    points = 0
    if valid:
        # Fixed, inspectable weights, not probability calibration or fitted thresholds.
        tests = (
            ("Broad altcoin participation", values["alt_breadth_up"] >= .70, 25),
            ("BTC positive over 30m", values["btc_ret_6"] >= .0015, 18),
            ("BTC positive over 60m", values["btc_ret_12"] >= .002, 12),
            ("BTC above its short EMA", values["btc_ema12_distance"] > 0, 12),
            ("BTC above its longer EMA", values["btc_ema48_distance"] > 0, 10),
            ("Directional movement", values["btc_eff_6"] >= .35, 8),
            ("BTC volume improving", values["btc_volume_z"] >= 0, 5),
            ("Buyer participation", values["btc_buy_ratio"] >= .50, 5),
            ("Positive average altcoin momentum", values["alt_mean_return_6"] >= .0015, 5),
        )
        for label, passed, weight in tests:
            if passed:
                points += weight
                reasons.append("+" + label)
            else:
                reasons.append("Not yet: " + label.lower())
        # Explicit multi-factor confirmation prevents a high weighted score alone
        # from publishing a premature research long-opportunity indication.
        ready = (points >= 68 and values["alt_breadth_up"] >= .70
                 and values["btc_ret_6"] >= .0015
                 and values["btc_ema12_distance"] > 0)
        improving = (points >= 43 and
                     (values["btc_ret_6"] > -.002 or values["alt_breadth_up"] >= .6))
        state = "LOOK_FOR_LONGS" if ready else "START_WATCHING" if improving else "STAY_OUT"
    else:
        state = "DATA_UNAVAILABLE"
        reasons = ["Current complete BTC and at least seven altcoin indicators required",
                   "Stale or incomplete observations cannot produce market alerts"]
    return {
        "candle_open_ms": candle_ms,
        "observed_at": iso(observed_ms),
        "ruleset_id": VERSION,
        "state": state,
        "opportunity_index": points if valid else None,
        "context_complete": bool(valid),
        "altcoins_covered": covered,
        "data_age_minutes": round(age / 60000, 2),
        "indicators": values,
        "reasons": reasons,
        "interpretation": "Research-only invitation to inspect charts; never an entry or trade instruction",
        "validation": "UNVALIDATED",
    }


def record(db: sqlite3.Connection, value: dict, observed_ms: int | None = None) -> bool:
    """Never reconstruct past alerts; only log observations when actually seen."""
    observed_ms = int(time.time() * 1000) if observed_ms is None else int(observed_ms)
    candle_ms = int(value["candle_open_ms"])
    if value["state"] == "DATA_UNAVAILABLE":
        return False
    # The first untouched full candle begins AFTER the real observation time.
    next_start = ((observed_ms // INTERVAL_MS) + 1) * INTERVAL_MS
    insert = db.execute("""INSERT OR IGNORE INTO opportunity_radar_observations
      (candle_open_ms,observed_at,scheduled_start_ms,ruleset_id,state,score,context_json,reasons_json)
      VALUES(?,?,?,?,?,?,?,?)""",
      (candle_ms, value["observed_at"], next_start, VERSION, value["state"],
       value["opportunity_index"], json.dumps(value["indicators"], allow_nan=False),
       json.dumps(value["reasons"], allow_nan=False)))
    if insert.rowcount:
        db.executemany("""INSERT OR IGNORE INTO opportunity_radar_outcomes
            (candle_open_ms,horizon_bars,status) VALUES(?,?,'PENDING')""",
            [(candle_ms, h) for h in HORIZONS])
    db.commit()
    return insert.rowcount == 1


def settle(db: sqlite3.Connection, candles: dict, now_ms: int | None = None) -> int:
    """Measure unseen opportunity windows from full, contiguous future candles."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    btc = candles.get("BTCUSDT")
    if btc is None or btc.empty:
        return 0
    last_btc = int(btc.index.max())
    outstanding = db.execute("""SELECT o.candle_open_ms,o.scheduled_start_ms,
       u.horizon_bars FROM opportunity_radar_observations o
       JOIN opportunity_radar_outcomes u USING(candle_open_ms)
       WHERE u.status='PENDING'""").fetchall()
    settled = 0
    for signal, first, horizon in outstanding:
        required = [first + j * INTERVAL_MS for j in range(horizon)]
        if required[-1] > last_btc:
            continue
        if not all(t in btc.index for t in required):
            status, result = "INVALID_DATA", {"reason": "MISSING_BTC_CANDLE"}
        else:
            alt_results = []
            first_btc = float(btc.loc[first, "open"])
            if first_btc <= 0:
                continue
            bw = btc.loc[required]
            btc_end_pct = 100 * (float(bw.iloc[-1]["close"]) / first_btc - 1)
            btc_peak_pct = 100 * (float(bw["high"].max()) / first_btc - 1)
            btc_dip_pct = 100 * (float(bw["low"].min()) / first_btc - 1)
            for symbol, frame in candles.items():
                if symbol == "BTCUSDT" or frame.empty or not all(t in frame.index for t in required):
                    continue
                start = float(frame.loc[first, "open"])
                if start <= 0:
                    continue
                window = frame.loc[required]
                alt_results.append({
                    "symbol": symbol,
                    "end_pct": 100 * (float(window.iloc[-1]["close"]) / start - 1),
                    "peak_pct": 100 * (float(window["high"].max()) / start - 1),
                })
            if len(alt_results) < MIN_ALTS:
                status, result = "INVALID_DATA", {"reason": "INSUFFICIENT_COMPLETE_ALTS",
                                                  "covered": len(alt_results)}
            else:
                positive = sum(x["end_pct"] > 0 for x in alt_results) / len(alt_results)
                peak_share = sum(x["peak_pct"] >= .5 for x in alt_results) / len(alt_results)
                median_end = statistics.median(x["end_pct"] for x in alt_results)
                # Predeclared descriptive market-window label, NOT a trade win.
                # Require broad upward opportunity and some persistence, with
                # rejection of large BTC drawdown before the measured high.
                favourable = (peak_share >= .60 and positive >= .60
                              and (btc_peak_pct >= .40 or median_end >= .35)
                              and btc_dip_pct >= -.90)
                result = {
                    "horizon_minutes": horizon * 5,
                    "btc_end_pct": round(btc_end_pct, 5),
                    "btc_peak_pct": round(btc_peak_pct, 5),
                    "btc_dip_pct": round(btc_dip_pct, 5),
                    "alt_positive_fraction": round(positive, 5),
                    "alt_peak_half_pct_fraction": round(peak_share, 5),
                    "alt_median_end_pct": round(median_end, 5),
                    "altcoin_count": len(alt_results),
                    "favourable_long_window": bool(favourable),
                    "definition": "Breadth + upside + persistence + downside; no trade fills",
                    "first_future_candle_ms": first,
                    "last_future_candle_ms": required[-1],
                }
                status = "COMPLETE"
        db.execute("""UPDATE opportunity_radar_outcomes
           SET status=?,result_json=?,evaluated_at=?
           WHERE candle_open_ms=? AND horizon_bars=? AND status='PENDING'""",
           (status, json.dumps(result), iso(now_ms), signal, horizon))
        settled += 1
    db.commit()
    return settled


def summary(db: sqlite3.Connection) -> dict:
    counts = db.execute("""SELECT state,COUNT(*) FROM opportunity_radar_observations
                          GROUP BY state""").fetchall()
    items = db.execute("""SELECT o.candle_open_ms,o.state,u.horizon_bars,
        u.result_json FROM opportunity_radar_observations o
        JOIN opportunity_radar_outcomes u USING(candle_open_ms)
        WHERE u.status='COMPLETE'
        ORDER BY o.candle_open_ms""").fetchall()
    evidence = []
    for horizon in HORIZONS:
        # Disjoint horizons per cohort, so neighbouring 5m alerts aren't
        # silently counted as independent experiments.
        last_start = -10**30
        grouped = {}
        for signal, state, h, raw in items:
            if h != horizon or signal - last_start < horizon * INTERVAL_MS:
                continue
            last_start = signal
            values = json.loads(raw)
            d = grouped.setdefault(state, {"evaluated":0,"favourable":0,
                                            "btc_mean_end_pct":0.,
                                            "days":set()})
            d["evaluated"] += 1
            d["favourable"] += int(values["favourable_long_window"])
            d["btc_mean_end_pct"] += values["btc_end_pct"]
            d["days"].add(iso(signal).split("T")[0])
        for state, v in grouped.items():
            evidence.append({
                "state":state,"horizon_minutes":horizon*5,
                "spaced_observations":v["evaluated"],
                "distinct_days":len(v["days"]),
                "favourable_windows":v["favourable"],
                "favourable_rate":round(v["favourable"]/v["evaluated"],4),
                "avg_btc_end_pct":round(v["btc_mean_end_pct"]/v["evaluated"],4),
                "validated":False,
            })
    return {
        "ruleset_id": VERSION,
        "observations_by_state": dict(counts),
        "comparison": evidence,
        "meaning": "Which saved alerts preceded broad upward windows, compared to STAY_OUT",
        "warning": "Exploratory, not calibrated. Few days of correlated market data cannot validate alerts.",
        "validation": "UNVALIDATED",
    }


def pg_install(cur) -> None:
    cur.execute(PG_OBS_DDL)
    cur.execute(PG_OUT_DDL)


def pg_sync(db: sqlite3.Connection, cur) -> dict:
    from psycopg.types.json import Jsonb
    obs = db.execute("""SELECT candle_open_ms,observed_at,scheduled_start_ms,
        ruleset_id,state,score,context_json,reasons_json
        FROM opportunity_radar_observations ORDER BY candle_open_ms DESC LIMIT 600""").fetchall()
    if obs:
        rows = []
        for signal, observed, first, rules, state, score, ctx, why in obs:
            payload = {"candle_open_ms":signal,"observed_at":observed,
                       "scheduled_start_ms":first,"ruleset_id":rules,
                       "state":state,"score":score,"indicators":json.loads(ctx),
                       "reasons":json.loads(why)}
            rows.append((PROVIDER, signal, Jsonb(payload)))
        cur.executemany("""INSERT INTO btc_opportunity_radar_v1(venue,candle_open_ms,payload)
            VALUES(%s,%s,%s) ON CONFLICT(venue,candle_open_ms) DO NOTHING""", rows)
    outcomes = db.execute("""SELECT candle_open_ms,horizon_bars,status,
          result_json,evaluated_at FROM opportunity_radar_outcomes
          ORDER BY candle_open_ms DESC LIMIT 2400""").fetchall()
    if outcomes:
        rows = [(PROVIDER,signal,h,status,
                 Jsonb(json.loads(result)) if result else None,evaluated)
                for signal,h,status,result,evaluated in outcomes]
        cur.executemany("""INSERT INTO btc_opportunity_outcomes_v1
          (venue,candle_open_ms,horizon_bars,status,payload,evaluated_at)
          VALUES(%s,%s,%s,%s,%s,%s)
          ON CONFLICT(venue,candle_open_ms,horizon_bars) DO UPDATE SET
             status=EXCLUDED.status,payload=EXCLUDED.payload,
             evaluated_at=EXCLUDED.evaluated_at,updated_at=now()
          WHERE btc_opportunity_outcomes_v1.status='PENDING'
            AND EXCLUDED.status IN ('COMPLETE','INVALID_DATA')""", rows)
    return {"observations":len(obs),"outcomes":len(outcomes)}


def pg_restore(db: sqlite3.Connection, cur) -> dict:
    cur.execute("""SELECT payload FROM btc_opportunity_radar_v1 WHERE venue=%s
                   ORDER BY candle_open_ms""", (PROVIDER,))
    observations = cur.fetchall()
    cur.execute("""SELECT candle_open_ms,horizon_bars,status,payload,evaluated_at
        FROM btc_opportunity_outcomes_v1 WHERE venue=%s ORDER BY candle_open_ms""",
        (PROVIDER,))
    outcomes = cur.fetchall()
    for (p,) in observations:
        if isinstance(p,str):
            p = json.loads(p)
        db.execute("""INSERT OR IGNORE INTO opportunity_radar_observations
           (candle_open_ms,observed_at,scheduled_start_ms,ruleset_id,state,score,
            context_json,reasons_json) VALUES(?,?,?,?,?,?,?,?)""",
           (p["candle_open_ms"],p["observed_at"],p["scheduled_start_ms"],
            p["ruleset_id"],p["state"],p["score"],json.dumps(p["indicators"]),
            json.dumps(p["reasons"])))
    for signal,h,status,payload,at in outcomes:
        if isinstance(payload,str):
            payload=json.loads(payload)
        db.execute("""INSERT INTO opportunity_radar_outcomes
          (candle_open_ms,horizon_bars,status,result_json,evaluated_at)
          VALUES(?,?,?,?,?)
          ON CONFLICT(candle_open_ms,horizon_bars) DO UPDATE SET
          status=excluded.status,result_json=excluded.result_json,
          evaluated_at=excluded.evaluated_at
          WHERE opportunity_radar_outcomes.status='PENDING'
            AND excluded.status IN ('COMPLETE','INVALID_DATA')""",
           (signal,h,status,json.dumps(payload) if payload is not None else None,at))
    db.commit()
    return {"observations":len(observations),"outcomes":len(outcomes)}
