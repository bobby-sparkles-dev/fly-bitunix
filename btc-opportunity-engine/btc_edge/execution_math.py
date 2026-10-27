"""Explain net R for a minimum-distance BTC spot bracket trade.

Pure math, independent of exchange data and model training.
Does NOT claim a real-world win rate or recommend an entry.
"""
from __future__ import annotations
from .config import Settings


def minimum_stop_geometry(settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    if settings.min_stop_pct <= 0 or settings.reward_to_risk <= 0:
        raise ValueError("Invalid trade geometry")
    slip = settings.slippage_bps_per_side / 10_000
    fee = settings.fee_bps_per_side / 10_000
    # Using a notional market candle-open price of 1.0 avoids rounding artifacts.
    raw_open = 1.0
    entry = raw_open * (1 + slip)
    risk = entry * settings.min_stop_pct
    stop = entry - risk
    target = entry + settings.reward_to_risk * risk

    def net_r(raw_exit: float) -> float:
        exit_fill = raw_exit * (1 - slip)
        net = exit_fill - entry - fee * (entry + exit_fill)
        return net / risk

    target_net_r = net_r(target)
    stop_net_r = net_r(stop)
    # Cost-free gross reward : risk differs from actual net reward : risk.
    breakeven_winrate = (-stop_net_r / (target_net_r - stop_net_r)
                         if target_net_r > 0 and stop_net_r < 0 else None)
    raw_exit_breakeven = entry * (1 + fee) / ((1 - slip) * (1 - fee))
    return {
        "example_assumes_min_stop": True,
        "holding_period_minutes": settings.horizon_bars * 5,
        "stop_distance_bps_from_filled_entry": round(settings.min_stop_pct * 10000, 3),
        "target_distance_bps_from_filled_entry": round(
            settings.min_stop_pct * settings.reward_to_risk * 10000, 3),
        "round_trip_fee_slippage_bps_approx": round(
            2 * (settings.fee_bps_per_side + settings.slippage_bps_per_side), 2),
        "break_even_raw_move_bps": round((raw_exit_breakeven - 1) * 10000, 3),
        "gross_target_move_from_raw_open_bps": round((target - 1) * 10000, 3),
        "gross_stop_move_from_raw_open_bps": round((stop - 1) * 10000, 3),
        "net_win_r_if_target_hit": round(target_net_r, 4),
        "net_loss_r_if_stop_hit": round(stop_net_r, 4),
        "breakeven_target_hit_rate_if_only_target_or_stop": (
            round(breakeven_winrate, 4) if breakeven_winrate is not None else None),
        "caveat": ("Illustrative minimum-stop fills only. Actual ATR may widen stops. "
                   "Time exits, price gaps, spreads and different exchange fees alter results.")
    }
