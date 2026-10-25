"""Regression tests for BTC net-profit label and cost audit.

A flat price has no gross gain, but fees/slippage make it a net loser.
These assertions prevent accidentally teaching the model a falsely low/high rate.
"""
import numpy as np
import pandas as pd
from btc_edge.config import Settings, INTERVAL_MS
from btc_edge.outcomes import build_outcomes


def flat_example():
    times=np.arange(16,dtype=np.int64)*INTERVAL_MS
    bars=pd.DataFrame({
        "open":[80000.0]*16,
        "high":[80015.0]*16,
        "low":[79985.0]*16,
        "close":[80000.0]*16
    },index=times)
    features=pd.DataFrame({"atr_at_decision":[80.0]*16},index=times)
    return bars,features


def test_flat_market_is_gross_breakeven_but_net_loss():
    bars,features=flat_example()
    out=build_outcomes(bars,features,Settings())
    row=out.iloc[3]
    assert row["exit_reason"]=="TIME"
    assert row["gross_r"]==0.0
    assert row["net_r"]<0
    assert row["cost_r"]>0.7
    assert row["long_win"]==0
    assert abs(row["gross_r"]-row["cost_r"]-row["net_r"])<1e-10


def test_profitable_price_move_can_reach_target_with_costs():
    bars,features=flat_example()
    # For decision candle 3, move during next 5-minute candle; price
    # never touches the stop, even though it crosses the target.
    bars.iloc[4,bars.columns.get_loc("high")]=80400.0
    out=build_outcomes(bars,features,Settings())
    row=out.iloc[3]
    assert row["exit_reason"]=="TARGET"
    assert row["long_win"]==1
    assert 0<row["net_r"]<row["gross_r"]
    assert row["cost_r"]>0


def test_reduced_fees_always_help_same_flat_trade():
    bars,features=flat_example()
    full=build_outcomes(bars,features,Settings()).iloc[3]
    free=build_outcomes(bars,features,
                        Settings(fee_bps_per_side=0,slippage_bps_per_side=0)).iloc[3]
    assert full["net_r"]<free["net_r"]
    assert free["net_r"]==0.0
