"""Forward opportunity radar tests: no orders, no hindsight, no fake accuracy."""
import json
import sqlite3

import pandas as pd

from btc_edge.config import INTERVAL_MS
from btc_edge.opportunity_radar import (
    HORIZONS, install, reading, record, settle, summary,
)


def example():
    db = sqlite3.connect(":memory:")
    install(db)
    n = 90
    idx = pd.Index([i * INTERVAL_MS for i in range(n + 1)], name="open_time")
    features = pd.DataFrame({
        "btc_ret_6": [.004] * len(idx),
        "btc_ret_12": [.006] * len(idx),
        "btc_eff_6": [.6] * len(idx),
        "btc_ema12_distance": [.004] * len(idx),
        "btc_ema48_distance": [.005] * len(idx),
        "btc_volume_z": [.8] * len(idx),
        "btc_buy_ratio": [.55] * len(idx),
        "alt_breadth_up": [.9] * len(idx),
        "alt_mean_return_6": [.005] * len(idx),
        "btc_atr_pct": [.002] * len(idx),
    }, index=idx)
    for coin in ("eth","sol","xrp","bnb","ada","doge","link","avax","trx","ltc"):
        features[coin + "_ret_6"] = .006
    score = {"timestamp_open_ms":n * INTERVAL_MS,
             "status": "RESEARCH_ONLY"}
    observed = (n + 1) * INTERVAL_MS + 65_000
    cols = ["open", "high", "low", "close"]
    raw = [[100., 100.2, 99.8, 100.] for _ in idx]
    coins = {symbol:pd.DataFrame(raw,index=idx,columns=cols)
             for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT",
                            "BNBUSDT","ADAUSDT","DOGEUSDT","LINKUSDT",
                            "AVAXUSDT","TRXUSDT","LTCUSDT")}
    return db,features,score,observed,coins


def extend(coins, first_ms, count, end=101.0):
    for symbol, df in coins.items():
        for j in range(count):
            t = first_ms + j * INTERVAL_MS
            df.loc[t] = [100., 101.5, 99.75, end]


def test_long_radar_is_not_a_trading_approval():
    db,features,score,observed,coins = example()
    value = reading(features,score,observed)
    assert value["state"] == "LOOK_FOR_LONGS"
    assert value["validation"] == "UNVALIDATED"
    assert "trade instruction" in value["interpretation"]
    assert record(db,value,observed)
    assert len(db.execute("SELECT * FROM opportunity_radar_outcomes").fetchall()) == 4
    # Nothing that occurred before observation can settle a later window.
    assert settle(db,coins,observed) == 0
    assert summary(db)["comparison"] == []


def test_forecasts_immutable_and_future_labels_separate():
    db,features,score,observed,coins = example()
    value = reading(features,score,observed)
    assert record(db,value,observed)
    original = db.execute("""SELECT state,score,context_json FROM
                    opportunity_radar_observations""").fetchone()
    features.loc[score["timestamp_open_ms"], "alt_breadth_up"] = 0
    assert not record(db,reading(features,score,observed),observed)
    assert db.execute("""SELECT state,score,context_json FROM
                    opportunity_radar_observations""").fetchone() == original
    first = ((observed // INTERVAL_MS) + 1) * INTERVAL_MS
    extend(coins,first,12,end=101.0)
    assert settle(db,coins,observed+12*INTERVAL_MS) == 2
    outcomes = db.execute("""SELECT horizon_bars,status,result_json FROM
                    opportunity_radar_outcomes ORDER BY horizon_bars""").fetchall()
    assert [x[1] for x in outcomes] == ["COMPLETE","COMPLETE","PENDING","PENDING"]
    assert all(json.loads(x[2])["favourable_long_window"] for x in outcomes[:2])
    assert not record(db,value,observed)
    assert db.execute("""SELECT state,score,context_json FROM
                    opportunity_radar_observations""").fetchone() == original
    assert settle(db,coins,observed+12*INTERVAL_MS) == 0


def test_flat_market_is_not_a_favourable_long_window():
    db,features,score,observed,coins = example()
    value = reading(features,score,observed)
    assert record(db,value,observed)
    first = ((observed // INTERVAL_MS) + 1) * INTERVAL_MS
    extend(coins,first,6,end=100.)
    assert settle(db,coins,observed+6*INTERVAL_MS) == 1
    result = json.loads(db.execute("""SELECT result_json FROM
         opportunity_radar_outcomes WHERE horizon_bars=6""").fetchone()[0])
    assert not result["favourable_long_window"]


def test_missing_market_context_never_shows_long_alert():
    db,features,score,observed,coins = example()
    for coin in ("eth","sol","xrp","bnb"):
        features.loc[score["timestamp_open_ms"],coin+"_ret_6"] = float("nan")
    value=reading(features,score,observed)
    assert value["state"] == "DATA_UNAVAILABLE"
    assert not record(db,value,observed)
    stale=reading(features,score,observed+45*60_000)
    assert stale["state"] == "DATA_UNAVAILABLE"


def test_weak_market_stays_out_despite_valid_features():
    db,features,score,observed,coins = example()
    i=score["timestamp_open_ms"]
    features.loc[i,["btc_ret_6","btc_ret_12","btc_ema12_distance",
                    "btc_ema48_distance","alt_breadth_up",
                    "alt_mean_return_6"]] = [-.01,-.015,-.01,-.02,.1,-.02]
    value=reading(features,score,observed)
    assert value["state"]=="STAY_OUT"
    assert value["opportunity_index"]<43
