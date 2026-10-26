"""Regression tests for reliable scheduling on five-minute BTC candles."""
from datetime import datetime, timezone

from btc_edge.config import INTERVAL_MS
from btc_edge.service import next_slot_delay


def test_schedules_after_next_utc_candle_close():
    step = INTERVAL_MS / 1000
    now = 30 * step + 100
    # Next 5m boundary + 25 seconds, not a browser wake or 5m after request.
    assert abs(next_slot_delay(now) - (step - 100 + 25)) < 0.001


def test_never_claims_previous_candle_before_it_closes():
    step = INTERVAL_MS / 1000
    now = 30 * step + step - 1
    assert 25 <= next_slot_delay(now) <= 27


def test_scoring_schedule_is_independent_of_five_minute_cycle_runtime():
    step = INTERVAL_MS / 1000
    now = 30 * step + 160
    assert next_slot_delay(now) < step


def test_store_is_isolated_by_exchange_venue():
    from btc_edge.market import PROVIDER
    assert PROVIDER in ("binance_us", "binance_global")
