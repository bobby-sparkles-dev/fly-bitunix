"""Conservative research defaults. No trading credentials or execution APIs."""
from dataclasses import dataclass

INTERVAL_MS = 300_000  # 5 minutes
SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT", "TRXUSDT",
)

@dataclass(frozen=True)
class Settings:
    horizon_bars: int = 6  # 30 minutes
    training_days: int = 30  # train plus chronologically held-out test
    test_days: int = 7
    half_life_days: int = 14
    stop_atr_multiple: float = 1.0
    min_stop_pct: float = 0.003
    reward_to_risk: float = 1.25
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 2.0
    tradeability_threshold: float = 0.62
    long_threshold: float = 0.62
    min_test_trades: int = 30
    max_data_staleness_minutes: int = 15
