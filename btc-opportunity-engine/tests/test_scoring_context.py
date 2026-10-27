"""Latest-candle research score cannot silently trail BTC by stale altcoins."""
import json
import pandas as pd
from btc_edge.config import INTERVAL_MS
from btc_edge.learning import latest_score


def model(path):
    cols = ["btc_ret_1", "hour_sin", "eth_ret_6"]
    bundle = {"features": cols, "means": [0., 0., 0.],
              "scales": [1., 1., 1.], "coefficients": [0., 0., 0.],
              "intercept": 0.0}
    path.mkdir(parents=True, exist_ok=True)
    (path / "latest.json").write_text(json.dumps({
        "model_id":"test", "models":{"tradeable":bundle, "long_win":bundle},
        "settings":{"tradeability_threshold":0.40, "long_threshold":0.40,
                    "max_data_staleness_minutes":15},
        "report":{"meets_research_gate":False}
    }))


def test_latest_btc_candle_scored_with_missing_alt_but_not_eligible(tmp_path):
    model(tmp_path)
    t = 500 * INTERVAL_MS
    df = pd.DataFrame({"btc_ret_1":[0.01,0.02],
                       "hour_sin":[0.,0.],
                       "eth_ret_6":[0.0,float("nan")]},
                      index=[t-INTERVAL_MS,t])
    score = latest_score(df, tmp_path, now_ms=t+INTERVAL_MS+60_000)
    assert score["timestamp_open_ms"] == t
    assert score["status"] == "PARTIAL_CONTEXT"
    assert score["context_complete"] is False
    assert score["candidate"] is False
    assert score["missing_alt_features"] == ["eth_ret_6"]
    assert score["data_age_minutes"] == 1.0


def test_complete_inputs_score_latest_and_allow_research_candidate(tmp_path):
    model(tmp_path)
    t = 500 * INTERVAL_MS
    df = pd.DataFrame({"btc_ret_1":[0.02], "hour_sin":[0.],
                       "eth_ret_6":[0.01]}, index=[t])
    score = latest_score(df,tmp_path,now_ms=t+INTERVAL_MS+60_000)
    assert score["status"] == "RESEARCH_ONLY"
    assert score["context_complete"] is True
    assert score["candidate"] is True


def test_stale_btc_never_eligible_for_paper(tmp_path):
    model(tmp_path)
    t = 500 * INTERVAL_MS
    df = pd.DataFrame({"btc_ret_1":[0.02], "hour_sin":[0.],
                       "eth_ret_6":[0.01]}, index=[t])
    score = latest_score(df,tmp_path,now_ms=t+INTERVAL_MS+16*60_000)
    assert score["status"] == "STALE_DATA"
