"""Strictly backward-looking features aligned on closed 5m candles."""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import INTERVAL_MS


def load_candles(db, days: int = 95) -> dict[str, pd.DataFrame]:
    cutoff = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000) - days * 86_400_000
    raw = pd.read_sql_query("SELECT * FROM candles WHERE open_time>=? ORDER BY symbol,open_time", db,
                            params=(cutoff,))
    if raw.empty:
        raise ValueError("No candle data. Run ingest first.")
    result = {}
    for symbol, data in raw.groupby("symbol"):
        data = data.set_index("open_time").sort_index()
        result[symbol] = data
    return result


def _efficiency(close: pd.Series, n: int) -> pd.Series:
    distance = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n).sum()
    return distance / path.replace(0, np.nan)


def build_features(candles: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if "BTCUSDT" not in candles:
        raise ValueError("BTCUSDT is required")
    btc = candles["BTCUSDT"].sort_index()
    if len(btc) < 80:
        raise ValueError("At least 80 BTC candles are required")
    idx = btc.index
    o, h, l, c, v = (btc[k].astype(float) for k in ("open", "high", "low", "close", "volume"))
    ret = c.pct_change()
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema48 = c.ewm(span=48, adjust=False).mean()
    f = pd.DataFrame(index=idx)
    f["btc_ret_1"] = ret
    for n in (3, 6, 12, 24):
        f[f"btc_ret_{n}"] = c.pct_change(n)
    f["btc_eff_6"] = _efficiency(c, 6)
    f["btc_eff_12"] = _efficiency(c, 12)
    f["btc_volatility_12"] = ret.rolling(12).std()
    f["btc_volatility_48"] = ret.rolling(48).std()
    f["btc_vol_contraction"] = f["btc_volatility_12"] / f["btc_volatility_48"].replace(0, np.nan)
    f["btc_volume_z"] = (v - v.rolling(48).mean()) / v.rolling(48).std().replace(0, np.nan)
    f["btc_buy_ratio"] = btc["taker_buy_volume"].astype(float) / v.replace(0, np.nan)
    f["btc_atr_pct"] = atr / c
    f["btc_ema12_distance"] = c / ema12 - 1
    f["btc_ema48_distance"] = c / ema48 - 1
    f["btc_range_position"] = (c - l.rolling(24).min()) / (h.rolling(24).max() - l.rolling(24).min()).replace(0, np.nan)
    f["btc_body_ratio"] = (c - o) / (h - l).replace(0, np.nan)
    alt_ret6 = []
    alt_ret12 = []
    alt_volz = []
    for symbol in sorted(s for s in candles if s != "BTCUSDT"):
        alt = candles[symbol].reindex(idx)  # Missing bars STAY missing, never forward-filled.
        ac = alt["close"].astype(float)
        av = alt["volume"].astype(float)
        r6, r12 = ac.pct_change(6, fill_method=None), ac.pct_change(12, fill_method=None)
        label = symbol.removesuffix("USDT").lower()
        f[f"{label}_ret_6"] = r6
        f[f"{label}_ret_12"] = r12
        f[f"{label}_volume_z"] = (av - av.rolling(48).mean()) / av.rolling(48).std().replace(0, np.nan)
        f[f"{label}_relative_12"] = r12 - f["btc_ret_12"]
        alt_ret6.append(r6)
        alt_ret12.append(r12)
        alt_volz.append(f[f"{label}_volume_z"])
    if alt_ret6:
        alt_return_frame = pd.concat(alt_ret6, axis=1)
        f["alt_breadth_up"] = (alt_return_frame > 0).where(alt_return_frame.notna()).mean(axis=1)
        f["alt_mean_return_6"] = alt_return_frame.mean(axis=1)
        f["alt_dispersion_6"] = alt_return_frame.std(axis=1, ddof=0)
        f["alt_mean_volume_z"] = pd.concat(alt_volz, axis=1).mean(axis=1)
    # Optional clock/time-of-week effects; based on known timestamp only.
    utc = pd.to_datetime(idx, unit="ms", utc=True)
    hour = utc.hour + utc.minute / 60
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    f["weekend"] = (utc.dayofweek >= 5).astype(float)
    f["atr_at_decision"] = atr  # USED ONLY FOR OUTCOME LABELS, never as model feature.
    # Filter gaps: contiguous BTC bars must be present to calculate valid rolling features.
    continuity = pd.Series(idx, index=idx).diff().eq(INTERVAL_MS).rolling(49).sum().eq(49)
    f = f.where(continuity)
    return f.replace([np.inf, -np.inf], np.nan)


def model_columns(feature_frame: pd.DataFrame) -> list[str]:
    return [k for k in feature_frame.columns if k != "atr_at_decision"]
