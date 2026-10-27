from btc_edge.config import Settings
from btc_edge.execution_math import minimum_stop_geometry


def test_minimum_stop_geometry_matches_current_fee_assumptions():
    audit = minimum_stop_geometry(Settings())
    assert audit["holding_period_minutes"] == 30
    assert audit["stop_distance_bps_from_filled_entry"] == 30
    assert audit["target_distance_bps_from_filled_entry"] == 37.5
    assert audit["round_trip_fee_slippage_bps_approx"] == 24
    assert 23.9 < audit["break_even_raw_move_bps"] < 24.2
    assert 0.50 < audit["net_win_r_if_target_hit"] < 0.53
    assert -1.74 < audit["net_loss_r_if_stop_hit"] < -1.72
    assert .76 < audit["breakeven_target_hit_rate_if_only_target_or_stop"] < .78


def test_cost_free_setup_reverts_to_gross_reward_to_risk():
    audit = minimum_stop_geometry(Settings(fee_bps_per_side=0,
                                           slippage_bps_per_side=0))
    assert audit["net_win_r_if_target_hit"] == 1.25
    assert audit["net_loss_r_if_stop_hit"] == -1
    assert abs(audit["breakeven_target_hit_rate_if_only_target_or_stop"]
               - 1/2.25) < .0001


def test_longer_timeframe_does_not_falsely_change_trade_geometry():
    a = minimum_stop_geometry(Settings())
    b = minimum_stop_geometry(Settings(horizon_bars=12))
    assert b["holding_period_minutes"] == 60
    assert b["net_win_r_if_target_hit"] == a["net_win_r_if_target_hit"]
    # Higher horizon must be evaluated on real history, not marked profitable by math alone.
