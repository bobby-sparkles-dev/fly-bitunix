"""Market dashboard must show the *stored* exchange candles, never synthetic live data."""
import sqlite3
from pathlib import Path
import pytest

from btc_edge.api_market import market_snapshot
from btc_edge.config import INTERVAL_MS, SYMBOLS
from btc_edge.storage import connect, upsert_candles


def test_no_database_fails_explicitly(tmp_path):
    with pytest.raises(FileNotFoundError):
        market_snapshot(tmp_path / "missing.sqlite", now_ms=10 * INTERVAL_MS)


def test_returns_completed_candles_and_freshness(tmp_path):
    path=tmp_path / "research.sqlite"
    with connect(path) as db:
        rows=[]
        for name in SYMBOLS:
            for n in range(50):
                opened = n * INTERVAL_MS
                rows.append((name, opened, opened+INTERVAL_MS-1, 100+n, 101+n,
                             99+n, 100.5+n, 1.0, 0.5, 12))
        upsert_candles(db, rows)
    clock=50 * INTERVAL_MS + 60_000
    result=market_snapshot(path, now_ms=clock)
    assert result["source"]=="backend_saved_completed_candles"
    assert not result["stale"]
    assert len(result["symbols"]["BTCUSDT"])==50
    assert len(result["symbols"]["ETHUSDT"])==20
    assert result["symbols"]["BTCUSDT"][-1][0]==49 * INTERVAL_MS
    assert result["age_minutes"]==1.0
    old=market_snapshot(path, now_ms=clock+20*60_000)
    assert old["stale"]
    assert old["age_minutes"]==21.0


def test_database_without_history_is_not_called_live(tmp_path):
    path=tmp_path/"research.sqlite"
    with connect(path):
        pass
    with pytest.raises(ValueError,match="not ready"):
        market_snapshot(path, now_ms=10 * INTERVAL_MS)
