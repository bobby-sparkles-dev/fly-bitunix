from dataclasses import replace
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import pytest

from btc_edge.config import INTERVAL_MS, Settings
from btc_edge.features import build_features, model_columns
from btc_edge.learning import latest_score, train, predict
from btc_edge.market import to_record
from btc_edge.outcomes import build_outcomes, non_overlapping_trades, trade_summary
from btc_edge.traders import extract_review_candidate


def synthetic(n=2300, seed=42):
    rng = np.random.default_rng(seed)
    idx = (1_780_000_000_000 + np.arange(n) * INTERVAL_MS).astype("int64")
    # Alternating regimes produce both positive and negative labels.
    regime = np.where((np.arange(n) // 95) % 4 == 0, 0.001,
                      np.where((np.arange(n) // 95) % 4 == 1, -0.0008, 0.0))
    shocks = rng.normal(regime, 0.0023, n)
    btc_close = 60_000 * np.exp(np.cumsum(shocks))
    def frame(prices, jitter=1.0):
        op = np.r_[prices[0], prices[:-1]]
        high = np.maximum(op, prices) * (1 + rng.uniform(0.0003, 0.0012, n))
        low = np.minimum(op, prices) * (1 - rng.uniform(0.0003, 0.0012, n))
        vol = rng.uniform(10, 20, n) * jitter
        return pd.DataFrame({"open": op, "high": high, "low": low, "close": prices,
                             "volume": vol, "taker_buy_volume": vol*rng.uniform(.35,.65,n),
                             "trade_count": rng.integers(100,500,n),
                             "close_time":idx+INTERVAL_MS-1}, index=pd.Index(idx,name="open_time"))
    return {"BTCUSDT":frame(btc_close),
            "ETHUSDT": frame(3_000 * np.exp(np.cumsum(shocks * .6 + rng.normal(0,.001,n))))}


def test_features_do_not_see_future():
    candles = synthetic(300)
    f1 = build_features(candles)
    t = candles["BTCUSDT"].index[240]
    candles["BTCUSDT"].loc[candles["BTCUSDT"].index[250], "close"] *= 1.9
    f2 = build_features(candles)
    pd.testing.assert_series_equal(f1.loc[t], f2.loc[t])
    assert "atr_at_decision" not in model_columns(f1)


def test_first_eligible_entry_uses_next_open_and_conservative_stops():
    c = synthetic(150)
    base = c["BTCUSDT"]
    f = build_features(c)
    i = 100
    # Chosen bar gets both stop and target at next candle, hence stop wins.
    base.loc[base.index[i + 1], "high"] = base["open"].iloc[i + 1] * 1.1
    base.loc[base.index[i + 1], "low"] = base["open"].iloc[i + 1] * .9
    outcomes = build_outcomes(base, f, Settings(horizon_bars=3))
    assert outcomes.loc[base.index[i], "exit_reason"] == "STOP"
    assert outcomes.loc[base.index[i], "long_win"] == 0
    assert base.index[-1] not in outcomes.index  # no complete future window


def test_nonoverlapping_paper_trades():
    t = [0,300_000,600_000,900_000]
    frame = pd.DataFrame({"exit_time": [600_000,600_000,900_000,1_200_000],
                          "net_r":[1,-1,1,-1]},index=t)
    selected = non_overlapping_trades(frame)
    assert list(selected.index) == [0,900_000]
    assert trade_summary(selected)["trades"] == 2


def test_market_record_format():
    bar = [1000,"1","3","0.5","2","10",301_000,"20",15,"6"]
    assert to_record("BTCUSDT",bar) == ("BTCUSDT",1000,301000,1.0,3.0,0.5,2.0,10.0,6.0,15)


def test_explicit_signal_only():
    timestamp = "2026-10-25T10:00:00+00:00"
    s = extract_review_candidate("BTC LONG entry: 72500 stop: 72100 target: 73500",
                                 "https://www.youtube.com/watch?v=test",timestamp,timestamp)
    assert s["status"] == "REVIEW_REQUIRED"
    with pytest.raises(ValueError):
        extract_review_candidate("Perhaps Bitcoin rises later", "https://example.com", timestamp, timestamp)
    with pytest.raises(ValueError):
        extract_review_candidate("BTC LONG entry: 72500 stop: 74100 target: 73500", "https://example.com", timestamp, timestamp)


def test_train_and_score_research_only(tmp_path):
    candles = synthetic()
    features = build_features(candles)
    settings = replace(Settings(), training_days=6, test_days=2, horizon_bars=6,
                       min_test_trades=9999)
    outcomes = build_outcomes(candles["BTCUSDT"], features, settings)
    payload = train(features, outcomes, settings, tmp_path)
    assert payload["deployment_mode"] == "PAPER_ONLY"
    assert payload["report"]["observations_training"] > 400
    assert payload["report"]["filtered_trades"]["trades"] >= 0
    assert not payload["report"]["meets_research_gate"]
    score = latest_score(features, tmp_path, now_ms=int(features.index.max())+INTERVAL_MS+60_000)
    assert score["status"] == "RESEARCH_ONLY"
    assert 0 <= score["p_tradeable"] <= 1
    assert 0 <= score["p_profitable_btc_long"] <= 1
