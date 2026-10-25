"""Recency-weighted, chronologically validated market and BTC-long models.

Models persist as plain JSON coefficients, never executable pickle.
All thresholds are precommitted constants, not tuned against the test sample.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.preprocessing import StandardScaler
from .config import INTERVAL_MS, Settings
from .features import model_columns
from .outcomes import non_overlapping_trades, trade_summary


def _fit(X: pd.DataFrame, y: pd.Series, age_days: np.ndarray, half_life: int) -> dict:
    if y.nunique() != 2:
        raise ValueError("Cannot fit: target has only one class in training window")
    scaler = StandardScaler().fit(X)
    weights = np.power(0.5, age_days / half_life)
    model = LogisticRegression(C=0.3, max_iter=600, random_state=42)
    model.fit(scaler.transform(X), y.astype(int), sample_weight=weights)
    return {"means": scaler.mean_.tolist(), "scales": scaler.scale_.tolist(),
            "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0])}


def predict(bundle: dict, X: pd.DataFrame) -> np.ndarray:
    cols = bundle["features"]
    z = ((X[cols].to_numpy(dtype=float) - np.array(bundle["means"]))
         / np.array(bundle["scales"])) @ np.array(bundle["coefficients"]) + bundle["intercept"]
    z = np.clip(z, -50, 50)
    return 1.0 / (1.0 + np.exp(-z))


def prepare(features: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    # Only information known at decision time is included in X.
    cols = model_columns(features)
    X = features[cols].join(outcomes[["tradeable", "long_win", "net_r", "gross_r", "cost_r", "exit_time"]], how="inner")
    return X.dropna().sort_index()


def train(features: pd.DataFrame, outcomes: pd.DataFrame, settings: Settings, output_dir: str | Path) -> dict:
    df = prepare(features, outcomes)
    if len(df) < 500:
        raise ValueError("At least 500 valid, aligned observations required")
    end = int(df.index.max())
    start = end - settings.training_days * 86_400_000
    test_start = end - settings.test_days * 86_400_000
    if settings.test_days >= settings.training_days:
        raise ValueError("test_days must be smaller than training_days")
    # Purge any train label that extends into the test period.
    training = df.loc[(df.index >= start) & (df.index + settings.horizon_bars * INTERVAL_MS < test_start)]
    test = df.loc[df.index >= test_start]
    if len(training) < 400 or len(test) < 150:
        raise ValueError("Insufficient train/test samples; ingest more history")
    cols = model_columns(features)
    trained_at = datetime.now(timezone.utc).isoformat()
    models = {}
    for label in ("tradeable", "long_win"):
        age_days = (int(training.index.max()) - training.index.to_numpy()) / 86_400_000
        params = _fit(training[cols], training[label], age_days, settings.half_life_days)
        models[label] = {**params, "features": cols, "label": label}
    # Compare added value from altcoin data without using the test period to choose weights.
    btc_cols = [c for c in cols if c.startswith("btc_") or c in ("hour_sin", "hour_cos", "weekend")]
    btc_only = _fit(training[btc_cols], training["long_win"], age_days,
                    settings.half_life_days)
    btc_only_model = {**btc_only, "features": btc_cols, "label": "long_win"}
    scored = test.copy()
    scored["p_long_btc_only"] = predict(btc_only_model, scored[btc_cols])
    scored["p_market"] = predict(models["tradeable"], scored[cols])
    scored["p_long"] = predict(models["long_win"], scored[cols])
    accepted = non_overlapping_trades(scored.loc[
        (scored.p_market >= settings.tradeability_threshold)
        & (scored.p_long >= settings.long_threshold)
    ])
    # A benchmark that only uses BTC features: simple known-price momentum.
    baseline = non_overlapping_trades(scored.loc[
        (scored.btc_ret_3 > 0) & (scored.btc_ema12_distance > 0)
    ])
    report = {
        "observations_training": len(training), "observations_test": len(test),
        "train_start_ms": int(training.index.min()), "train_end_ms": int(training.index.max()),
        "test_start_ms": int(test.index.min()), "test_end_ms": int(test.index.max()),
        "brier_market": round(float(brier_score_loss(test.tradeable, scored.p_market)), 5),
        "brier_long": round(float(brier_score_loss(test.long_win, scored.p_long)), 5),
        "brier_long_btc_only": round(float(brier_score_loss(test.long_win, scored.p_long_btc_only)), 5),
        "filtered_trades": trade_summary(accepted),
        "momentum_baseline": trade_summary(baseline),
        "all_possible_nonoverlapping": trade_summary(non_overlapping_trades(test)),
    }

    # Cost audit: explain why nearly every small, short-horizon BTC move may
    # fail a net-of-fees long label. Never use holdout data for training.
    train_long_base_rate = float(training["long_win"].mean())
    test_long_base_rate = float(test["long_win"].mean())
    report["cost_audit"] = {
        "label_definition": "Net-positive long using next-candle open, ATR/minimum stop, 1.25R target or time exit",
        "window_minutes": settings.horizon_bars * INTERVAL_MS // 60_000,
        "round_trip_fee_slippage_bps_approx": round(2 * (
            settings.fee_bps_per_side + settings.slippage_bps_per_side), 2),
        "minimum_stop_distance_bps": round(settings.min_stop_pct * 10_000, 2),
        "train_net_positive_fraction": round(train_long_base_rate, 5),
        "test_net_positive_fraction": round(test_long_base_rate, 5),
        "test_price_positive_before_costs_fraction":
            round(float((test["gross_r"] > 0).mean()), 5),
        "test_tradeable_label_fraction": round(float(test["tradeable"].mean()), 5),
        "test_average_gross_r": round(float(test["gross_r"].mean()), 5),
        "test_average_cost_r": round(float(test["cost_r"].mean()), 5),
        "test_average_net_r": round(float(test["net_r"].mean()), 5),
        "test_market_threshold_passed_candles":
            int((scored["p_market"] >= settings.tradeability_threshold).sum()),
        "test_long_threshold_passed_candles":
            int((scored["p_long"] >= settings.long_threshold).sum()),
        "test_both_thresholds_passed_candles": int((
            (scored["p_market"] >= settings.tradeability_threshold) &
            (scored["p_long"] >= settings.long_threshold)).sum()),
        "test_average_predicted_long_probability": round(float(scored["p_long"].mean()), 5),
        "test_brier_constant_train_base_rate": round(float(
            np.mean((test["long_win"].astype(float) - train_long_base_rate) ** 2)), 5),
        "sample_warning": "5-minute observations have overlapping 30-minute futures; do not treat them as independent trades",
        "no_live_trade_authorisation": True,
    }

    result = report["filtered_trades"]
    benchmark = report["momentum_baseline"]
    # Deployment gating: this is STILL paper-only; short backtests do not authorize execution.
    report["meets_research_gate"] = bool(
        result["trades"] >= settings.min_test_trades
        and result["win_rate"] is not None and result["win_rate"] >= 0.55
        and result["mean_net_r"] is not None and result["mean_net_r"] > 0
        and benchmark["mean_net_r"] is not None
        and result["mean_net_r"] > benchmark["mean_net_r"]
    )
    payload = {"version": 1, "trained_at": trained_at, "horizon_bars": settings.horizon_bars,
               "settings": vars(settings), "models": models, "report": report,
               "deployment_mode": "PAPER_ONLY"}
    payload["model_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Keep immutable model snapshots, with a copy of the latest for CLI scoring.
    (out / f"model-{payload['model_id']}.json").write_text(json.dumps(payload, indent=2))
    (out / "latest.json").write_text(json.dumps(payload, indent=2))
    return payload


def latest_score(features: pd.DataFrame, model_dir: str | Path, now_ms: int | None = None) -> dict:
    path = Path(model_dir) / "latest.json"
    model = json.loads(path.read_text())
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    expected_cols = model["models"]["tradeable"]["features"]
    rows = features.dropna(subset=expected_cols)
    if rows.empty:
        raise ValueError("No complete feature vector available")
    t = int(rows.index.max())
    # The most recent candle must be closed; scoring latency measured from candle close.
    age_min = (now_ms - (t + INTERVAL_MS)) / 60_000
    settings = model["settings"]
    p_market = float(predict(model["models"]["tradeable"], rows.iloc[-1:])[0])
    p_long = float(predict(model["models"]["long_win"], rows.iloc[-1:])[0])
    candidate = (p_market >= settings["tradeability_threshold"]
                 and p_long >= settings["long_threshold"])
    if age_min < 0 or age_min > settings["max_data_staleness_minutes"]:
        status = "STALE_DATA"
    elif not model["report"]["meets_research_gate"]:
        status = "RESEARCH_ONLY"
    elif candidate:
        status = "PAPER_CANDIDATE"
    else:
        status = "WAIT"
    return {"model_id": model["model_id"], "timestamp_open_ms": t,
            "data_age_minutes": round(age_min, 2), "p_tradeable": round(p_market, 4),
            "p_profitable_btc_long": round(p_long, 4), "candidate": candidate,
            "status": status, "mode": "PAPER_ONLY", "out_of_sample": model["report"]}
