"""Point-in-time BTC long labels and conservative executable paper-trade outcomes."""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import INTERVAL_MS, Settings


def build_outcomes(btc: pd.DataFrame, features: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """At decision candle t close, enter at t+1 OPEN, evaluate bars t+1..t+H.

    If stop and target are touched during the same candle, charge the stop.
    Bar gaps and incomplete horizons receive no label.
    """
    btc = btc.sort_index()
    f = features.reindex(btc.index)
    result = pd.DataFrame(index=btc.index,
                          columns=["tradeable", "long_win", "net_r", "gross_r", "cost_r", "exit_time", "exit_reason"],
                          dtype=object)
    horizon = settings.horizon_bars
    if horizon < 1:
        raise ValueError("horizon_bars must be >= 1")
    values = btc[["open", "high", "low", "close"]].astype(float)
    t = list(btc.index)
    for i in range(0, len(t) - horizon):
        if i < 1 or not np.isfinite(f["atr_at_decision"].iat[i]):
            continue
        times = t[i:i + horizon + 1]
        if any(times[j + 1] - times[j] != INTERVAL_MS for j in range(horizon)):
            continue
        history = values.iloc[i + 1:i + horizon + 1]
        entry_open = float(history["open"].iat[0])
        atr = float(f["atr_at_decision"].iat[i])
        if atr <= 0 or entry_open <= 0:
            continue
        # Future label for market conditions, not a contemporaneous feature.
        future_closes = values["close"].iloc[i:i + horizon + 1]
        travel = float(future_closes.diff().abs().iloc[1:].sum())
        net_move = abs(float(future_closes.iloc[-1] - future_closes.iloc[0]))
        future_efficiency = net_move / travel if travel > 0 else 0.0
        future_range = float(history["high"].max() - history["low"].min())
        tradeable = int(future_efficiency >= 0.40 and future_range >= 0.8 * atr)

        # Conservative spot buy and exit execution assumptions.
        slip = settings.slippage_bps_per_side / 10_000
        fee = settings.fee_bps_per_side / 10_000
        entry = entry_open * (1 + slip)
        risk = max(settings.stop_atr_multiple * atr, entry * settings.min_stop_pct)
        stop = entry - risk
        target = entry + settings.reward_to_risk * risk
        exit_raw = float(history["close"].iat[-1])
        reason = "TIME"
        exit_ts = t[i + horizon]
        for offset, (_, bar) in enumerate(history.iterrows(), start=1):
            if float(bar["low"]) <= stop:
                exit_raw, reason, exit_ts = min(stop, float(bar["open"])), "STOP", t[i + offset]
                break
            if float(bar["high"]) >= target:
                exit_raw, reason, exit_ts = target, "TARGET", t[i + offset]
                break
        exit_fill = exit_raw * (1 - slip)
        net_pnl = exit_fill - entry - fee * (entry + exit_fill)
        net_r = net_pnl / risk
        # Counterfactual price movement with identical setup/exit, before fees
        # and slippage. This is a diagnostic ONLY, never a trained feature.
        gross_r = (exit_raw - entry_open) / risk
        friction_r = gross_r - net_r
        result.loc[t[i]] = (tradeable, int(net_r > 0), net_r, gross_r,
                            friction_r, exit_ts, reason)
    return result.dropna().astype({"tradeable": int, "long_win": int,
                                   "net_r": float, "gross_r": float, "cost_r": float, "exit_time": "int64"})


def non_overlapping_trades(rows: pd.DataFrame) -> pd.DataFrame:
    """Select chronological opportunities; never hold two simultaneous paper trades."""
    accepted = []
    previous_exit = -1
    for timestamp, row in rows.sort_index().iterrows():
        if int(timestamp) > previous_exit:
            accepted.append(timestamp)
            previous_exit = int(row["exit_time"])
    return rows.loc[accepted] if accepted else rows.iloc[:0]


def trade_summary(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {"trades": 0, "wins": 0, "win_rate": None, "mean_net_r": None,
                "total_net_r": 0.0, "avg_win_r": None, "avg_loss_r": None,
                "max_drawdown_r": None}
    net = rows["net_r"].astype(float)
    wins = net > 0
    equity = net.cumsum()
    peak = pd.concat([pd.Series([0.0]), equity.reset_index(drop=True)]).cummax().iloc[1:].to_numpy()
    dd = equity.to_numpy() - peak
    return {"trades": len(rows), "wins": int(wins.sum()),
            "win_rate": round(float(wins.mean()), 4),
            "mean_net_r": round(float(net.mean()), 4),
            "total_net_r": round(float(net.sum()), 3),
            "avg_win_r": round(float(net[wins].mean()), 4) if wins.any() else None,
            "avg_loss_r": round(float(net[~wins].mean()), 4) if (~wins).any() else None,
            "max_drawdown_r": round(float(min(0.0, dd.min())), 3)}
